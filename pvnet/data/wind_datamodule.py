""" Data module for pytorch lightning """
import glob
from datetime import datetime

import numpy as np
import torch
from lightning.pytorch import LightningDataModule
from ocf_datapipes.training.windnet import windnet_netcdf_datapipe
from ocf_datapipes.utils.consts import BatchKey
from ocf_datapipes.utils.utils import stack_np_examples_into_batch
from torch.utils.data import DataLoader
from torch.utils.data.datapipes._decorator import functional_datapipe
from torch.utils.data.datapipes.datapipe import IterDataPipe


def copy_batch_to_device(batch, device):
    """Moves a dict-batch of tensors to new device."""
    batch_copy = {}
    for k in list(batch.keys()):
        if isinstance(batch[k], torch.Tensor):
            batch_copy[k] = batch[k].to(device)
        else:
            batch_copy[k] = batch[k]
    return batch_copy


def batch_to_tensor(batch):
    """Moves numpy batch to a tensor"""
    for k in list(batch.keys()):
        if isinstance(batch[k], np.ndarray) and np.issubdtype(batch[k].dtype, np.number):
            batch[k] = torch.as_tensor(batch[k])
    return batch


def split_batches(batch):
    """Splits a single batch of data."""
    n_samples = batch[BatchKey.sensor].shape[0]
    keys = list(batch.keys())
    examples = [{} for _ in range(n_samples)]
    for i in range(n_samples):
        b = examples[i]
        for k in keys:
            if ("idx" in k.name) or ("channel_names" in k.name):
                b[k] = batch[k]
            else:
                b[k] = batch[k][i]
    return examples


@functional_datapipe("split_batches")
class BatchSplitter(IterDataPipe):
    """Pipeline step to split batches of data and yield single examples"""

    def __init__(self, source_datapipe: IterDataPipe):
        """Pipeline step to split batches of data and yield single examples"""
        self.source_datapipe = source_datapipe

    def __iter__(self):
        """Opens the NWP data"""
        for batch in self.source_datapipe:
            for example in split_batches(batch):
                yield example


class WindDataModule(LightningDataModule):
    """Datamodule for training pvnet and using pvnet pipeline in `ocf_datapipes`."""

    def __init__(
        self,
        configuration=None,
        batch_size=16,
        num_workers=0,
        prefetch_factor=None,
        train_period=[None, None],
        val_period=[None, None],
        test_period=[None, None],
        batch_dir=None,
    ):
        """Datamodule for training pvnet and using pvnet pipeline in `ocf_datapipes`.

        Can also be used with pre-made batches if `batch_dir` is set.


        Args:
            configuration: Path to datapipe configuration file.
            batch_size: Batch size.
            num_workers: Number of workers to use in multiprocess batch loading.
            prefetch_factor: Number of data will be prefetched at the end of each worker process.
            train_period: Date range filter for train dataloader.
            val_period: Date range filter for val dataloader.
            test_period: Date range filter for test dataloader.
            batch_dir: Path to the directory of pre-saved batches. Cannot be used together with
                'train/val/test_period'.

        """
        super().__init__()
        self.configuration = configuration
        self.batch_size = batch_size
        self.batch_dir = batch_dir

        if batch_dir is not None:
            if any([period != [None, None] for period in [train_period, val_period, test_period]]):
                raise ValueError("Cannot set `(train/val/test)_period` with presaved batches")

        self.train_period = [
            None if d is None else datetime.strptime(d, "%Y-%m-%d") for d in train_period
        ]
        self.val_period = [
            None if d is None else datetime.strptime(d, "%Y-%m-%d") for d in val_period
        ]
        self.test_period = [
            None if d is None else datetime.strptime(d, "%Y-%m-%d") for d in test_period
        ]

        self._common_dataloader_kwargs = dict(
            shuffle=False,  # shuffled in datapipe step
            batch_size=None,  # batched in datapipe step
            sampler=None,
            batch_sampler=None,
            num_workers=num_workers,
            collate_fn=None,
            pin_memory=False,
            drop_last=False,
            timeout=0,
            worker_init_fn=None,
            prefetch_factor=prefetch_factor,
            persistent_workers=False,
        )

    def _get_datapipe(self, start_time, end_time):
        data_pipeline = windnet_netcdf_datapipe(
            self.configuration,
            keys=["sensor", "nwp"],
        )

        data_pipeline = (
            data_pipeline.batch(self.batch_size)
            .map(stack_np_examples_into_batch)
            .map(batch_to_tensor)
        )
        return data_pipeline

    def _get_premade_batches_datapipe(self, subdir, shuffle=False):
        data_pipeline = windnet_netcdf_datapipe(
            config_filename=self.configuration,
            keys=["sensor", "nwp"],
            filenames=list(glob.glob(f"{self.batch_dir}/{subdir}/*.nc")),
        )
        if shuffle:
            data_pipeline = (
                data_pipeline.shuffle(buffer_size=100)
                .sharding_filter()
                # Split the batches and reshuffle them to be combined into new batches
                .split_batches()
                .shuffle(buffer_size=100 * self.batch_size)
            )
        else:
            data_pipeline = (
                data_pipeline.sharding_filter()
                # Split the batches so we can use any batch-size
                .split_batches()
            )

        data_pipeline = (
            data_pipeline.batch(self.batch_size)
            .map(stack_np_examples_into_batch)
            .map(batch_to_tensor)
        )

        return data_pipeline

    def train_dataloader(self):
        """Construct train dataloader"""
        if self.batch_dir is not None:
            datapipe = self._get_premade_batches_datapipe("train", shuffle=True)
        else:
            datapipe = self._get_datapipe(*self.train_period)
        return DataLoader(datapipe, **self._common_dataloader_kwargs)

    def val_dataloader(self):
        """Construct val dataloader"""
        if self.batch_dir is not None:
            datapipe = self._get_premade_batches_datapipe("val")
        else:
            datapipe = self._get_datapipe(*self.val_period)
        return DataLoader(datapipe, **self._common_dataloader_kwargs)

    def test_dataloader(self):
        """Construct test dataloader"""
        if self.batch_dir is not None:
            datapipe = self._get_premade_batches_datapipe("test")
        else:
            datapipe = self._get_datapipe(*self.test_period)
        return DataLoader(datapipe, **self._common_dataloader_kwargs)
