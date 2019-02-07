# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Detectron data loader. The design is generic and abstracted away from any
details of the minibatch. A minibatch is a dictionary of blob name keys and
their associated numpy (float32 or int32) ndarray values.

Outline of the data loader design:

loader thread\
loader thread \                    / GPU 1 enqueue thread -> feed -> EnqueueOp
...           -> minibatch queue ->  ...
loader thread /                    \ GPU N enqueue thread -> feed -> EnqueueOp
loader thread/

<---------------------------- CPU -----------------------------|---- GPU ---->

A pool of loader threads construct minibatches that are put onto the shared
minibatch queue. Each GPU has an enqueue thread that pulls a minibatch off the
minibatch queue, feeds the minibatch blobs into the workspace, and then runs
an EnqueueBlobsOp to place the minibatch blobs into the GPU's blobs queue.
During each fprop the first thing the network does is run a DequeueBlobsOp
in order to populate the workspace with the blobs from a queued minibatch.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import deque
from collections import OrderedDict
import logging
import numpy as np
import Queue
import signal
import threading
import time
import uuid

from caffe2.python import core, workspace

from core.config_rel import cfg
from roi_data.minibatch_rel import get_minibatch
from roi_data.minibatch_rel import get_minibatch_blob_names
from utils.coordinator import coordinated_get
from utils.coordinator import coordinated_put
from utils.coordinator import Coordinator
import utils.c2 as c2_utils

logger = logging.getLogger(__name__)


class RoIDataLoader(object):
    def __init__(
        self,
        split,
        roidb,
        landb,
        proposals,
        num_loaders=4,
        minibatch_queue_size=64,
        blobs_queue_capacity=8
    ):
        self._split = split
        self._roidb = roidb
        self._landb = landb
        self._proposals = proposals
        self._lock = threading.Lock()
        self._perm = deque(range(len(self._roidb)))
        self._cur = 0  # _perm cursor
        # The minibatch queue holds prepared training data in host (CPU) memory
        # When training with N > 1 GPUs, each element in the minibatch queue
        # is actually a partial minibatch which contributes 1 / N of the
        # examples to the overall minibatch
        self._minibatch_queue = Queue.Queue(maxsize=minibatch_queue_size)
        self._blobs_queue_capacity = blobs_queue_capacity
        # Random queue name in case one instantiates multple RoIDataLoaders
        self._loader_id = uuid.uuid4()
        self._blobs_queue_name = 'roi_blobs_queue_{}'.format(self._loader_id)
        # Loader threads construct (partial) minibatches and put them on the
        # minibatch queue
        self._num_loaders = num_loaders
        self._num_gpus = cfg.NUM_DEVICES
        self.coordinator = Coordinator()

        self._output_names = get_minibatch_blob_names(split)
        self._shuffle_roidb_inds()
        self.create_threads()

    def minibatch_loader_thread(self):
        """Load mini-batches and put them onto the mini-batch queue."""

        def put_blobs_into_queue(blobs):
            ordered_blobs = OrderedDict()
            for key in self.get_output_names():
                assert blobs[key].dtype in (np.int32, np.float32), \
                    'Blob {} of dtype {} must have dtype of ' \
                    'np.int32 or np.float32'.format(key, blobs[key].dtype)
                ordered_blobs[key] = blobs[key]
            coordinated_put(
                self.coordinator, self._minibatch_queue, ordered_blobs
            )

        with self.coordinator.stop_on_exception():
            while not self.coordinator.should_stop():
                blobs = self.get_next_minibatch()
                # Blobs must be queued in the order specified by
                # self.get_output_names
                if self._split == 'train':
                    put_blobs_into_queue(blobs)
                else:
                    for group_blobs in blobs:
                        put_blobs_into_queue(group_blobs)
        logger.info('Stopping mini-batch loading thread')

    def enqueue_blobs_thread(self, gpu_id, blob_names):
        """Transfer mini-batches from a mini-batch queue to a BlobsQueue."""
        with self.coordinator.stop_on_exception():
            while not self.coordinator.should_stop():
                if self._minibatch_queue.qsize == 0:
                    logger.warning('Mini-batch queue is empty')
                blobs = coordinated_get(self.coordinator, self._minibatch_queue)
                self.enqueue_blobs(gpu_id, blob_names, blobs.values())
                logger.debug(
                    'batch queue size {}'.format(self._minibatch_queue.qsize())
                )
            logger.info('Stopping enqueue thread')

    def get_next_minibatch(self):
        """
        Returns next blobs to be used for the next mini-batch queue
        """
        valid = False
        while not valid:
            db_inds = self._get_next_minibatch_inds()
            minibatch_db = [self._roidb[i] for i in db_inds]
            if self._split == 'train':
                if self._proposals is not None:
                    minibatch_proposals = [self._proposals[i] for i in db_inds]
                else:
                    minibatch_proposals = None
                blobs, valid = get_minibatch(
                    self._split,
                    self._landb, minibatch_db, db_inds, minibatch_proposals,
                    None)
            else:
                minibatch_proposals = self._proposals[db_inds[0]]
                num_proposals = len(minibatch_proposals['boxes_sbj'])
                if self._split == 'test':
                    group_size = cfg.TEST.GROUP_SIZE
                else:
                    group_size = cfg.VAL.GROUP_SIZE
                num_groups = 1
                if num_proposals > group_size:
                    num_groups = \
                        int(np.ceil(num_proposals / float(group_size)))
                blobs = []
                for i in range(num_groups):
                    group_proposals = dict(
                        boxes_sbj=minibatch_proposals[
                            'boxes_sbj'][i * group_size:(i + 1) * group_size],
                        boxes_obj=minibatch_proposals[
                            'boxes_obj'][i * group_size:(i + 1) * group_size],
                        boxes_rel=minibatch_proposals[
                            'boxes_rel'][i * group_size:(i + 1) * group_size],
                    )
                    group_proposals['subbatch_id'] = i
                    group_blobs, valid = get_minibatch(
                        self._split,
                        self._landb, minibatch_db, db_inds,
                        [group_proposals], None)
                    blobs.append(group_blobs)
        return blobs

    def _shuffle_roidb_inds(self):
        """Randomly permute the training roidb. Not thread safe."""
        self._perm = np.random.permutation(np.arange(len(self._roidb)))
        self._perm = deque(self._perm)
        self._cur = 0

    def _get_next_minibatch_inds(self):
        """Return the roidb indices for the next minibatch. Thread safe."""
        with self._lock:
            # We use a deque and always take the *first* IMS_PER_BATCH items
            # followed by *rotating* the deque so that we see fresh items
            # each time. If the length of _perm is not divisible by
            # IMS_PER_BATCH, then we end up wrapping around the permutation.
            db_inds = [self._perm[i] for i in range(cfg.TRAIN.IMS_PER_BATCH)]
            self._perm.rotate(-cfg.TRAIN.IMS_PER_BATCH)
            self._cur += cfg.TRAIN.IMS_PER_BATCH
            if self._cur >= len(self._perm):
                self._shuffle_roidb_inds()
        return db_inds

    def get_output_names(self):
        return self._output_names

    def enqueue_blobs(self, gpu_id, blob_names, blobs):
        """Put a mini-batch on a BlobsQueue."""
        assert len(blob_names) == len(blobs)
        t = time.time()
        dev = c2_utils.CudaDevice(gpu_id)
        queue_name = 'gpu_{}/{}'.format(gpu_id, self._blobs_queue_name)
        blob_names = ['gpu_{}/{}'.format(gpu_id, b) for b in blob_names]
        for (blob_name, blob) in zip(blob_names, blobs):
            workspace.FeedBlob(blob_name, blob, device_option=dev)
        logger.debug(
            'enqueue_blobs {}: workspace.FeedBlob: {}'.
            format(gpu_id, time.time() - t)
        )
        t = time.time()
        op = core.CreateOperator(
            'SafeEnqueueBlobs', [queue_name] + blob_names,
            blob_names + [queue_name + '_enqueue_status'],
            device_option=dev
        )
        workspace.RunOperatorOnce(op)
        logger.debug(
            'enqueue_blobs {}: workspace.RunOperatorOnce: {}'.
            format(gpu_id, time.time() - t)
        )

    def create_threads(self):
        # Create mini-batch loader threads, each of which builds mini-batches
        # and places them into a queue in CPU memory
        self._workers = [
            threading.Thread(target=self.minibatch_loader_thread)
            for _ in range(self._num_loaders)
        ]

        # Create one BlobsQueue per GPU
        # (enqueue_blob_names are unscoped)
        enqueue_blob_names = self.create_blobs_queues()

        # Create one enqueuer thread per GPU
        self._enqueuers = [
            threading.Thread(
                target=self.enqueue_blobs_thread,
                args=(gpu_id, enqueue_blob_names)
            ) for gpu_id in range(self._num_gpus)
        ]

    def start(self, prefill=False):
        for w in self._workers + self._enqueuers:
            w.start()
        if prefill:
            logger.info('Pre-filling mini-batch queue...')
            while not self._minibatch_queue.full():
                logger.info(
                    '  [{:d}/{:d}]'.format(
                        self._minibatch_queue.qsize(),
                        self._minibatch_queue.maxsize
                    )
                )
                time.sleep(0.1)
                # Detect failure and shutdown
                if self.coordinator.should_stop():
                    self.shutdown()
                    break

    def shutdown(self):
        self.coordinator.request_stop()
        self.coordinator.wait_for_stop()
        self.close_blobs_queues()
        for w in self._workers + self._enqueuers:
            w.join()

    def create_blobs_queues(self):
        """Create one BlobsQueue for each GPU to hold mini-batches."""
        for gpu_id in range(self._num_gpus):
            with c2_utils.GpuNameScope(gpu_id):
                workspace.RunOperatorOnce(
                    core.CreateOperator(
                        'CreateBlobsQueue', [], [self._blobs_queue_name],
                        num_blobs=len(self.get_output_names()),
                        capacity=self._blobs_queue_capacity
                    )
                )
        return self.create_enqueue_blobs()

    def close_blobs_queues(self):
        """Close a BlobsQueue."""
        for gpu_id in range(self._num_gpus):
            with core.NameScope('gpu_{}'.format(gpu_id)):
                workspace.RunOperatorOnce(
                    core.CreateOperator(
                        'CloseBlobsQueue', [self._blobs_queue_name], []
                    )
                )

    def create_enqueue_blobs(self):
        blob_names = self.get_output_names()
        enqueue_blob_names = [
            '{}_enqueue_{}'.format(b, self._loader_id) for b in blob_names
        ]
        for gpu_id in range(self._num_gpus):
            with c2_utils.NamedCudaScope(gpu_id):
                for blob in enqueue_blob_names:
                    workspace.CreateBlob(core.ScopedName(blob))
        return enqueue_blob_names

    def register_sigint_handler(self):
        def signal_handler(signal, frame):
            logger.info(
                'SIGINT: Shutting down RoIDataLoader threads and exiting...'
            )
            self.shutdown()

        signal.signal(signal.SIGINT, signal_handler)

    def minibatch_queue_size(self):
        return self._minibatch_queue.qsize()