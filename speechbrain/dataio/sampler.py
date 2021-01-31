"""PyTorch compatible samplers

These determine the order of iteration through a dataset.

Authors:
  * Aku Rouhe 2020
  * Samuele Cornell 2020
  * Ralf Leibold 2020
"""
import torch
import logging
from operator import itemgetter
from torch.utils.data import RandomSampler, DistributedSampler, Sampler
import numpy as np
import math
from typing import List
from speechbrain.dataio.dataset import DynamicItemDataset

logger = logging.getLogger(__name__)


class ReproducibleRandomSampler(RandomSampler):
    """
    A modification of RandomSampler which always returns the same values.

    Also look at `torch.utils.data.RandomSampler`. This has mostly
    the same behaviour and arguments, except for adding 'seed' and 'epoch' and
    not supporting 'generator'.

    Note
    ----
    Call `set_epoch` before every epoch. Otherwise, the sampler will produce the
    same sequence of indices every epoch.

    Arguments
    ---------
    seed : int
        The base seed to use for the random number generator. It is recommended
        to use a value which has a good mix of 0 and 1 bits.
    epoch : int
        The epoch to start at.

    Example
    -------
    >>> import torch
    >>> from speechbrain.utils.checkpoints import Checkpointer
    >>> from speechbrain.dataio.dataloader import SaveableDataLoader
    >>> # An example "dataset"
    >>> dataset = torch.arange(10).unsqueeze(1)
    >>> # Create the random sampler:
    >>> sampler = ReproducibleRandomSampler(dataset)
    >>> dataloader = SaveableDataLoader(dataset, sampler = sampler,
    ...     num_workers = 3)
    >>> # Setup the checkpointer.
    >>> # Note that the sampler doesn't need to be saved itself.
    >>> tmpdir = getfixture('tmpdir')
    >>> checkpointer = Checkpointer(tmpdir, {"dataloader": dataloader})
    >>> # Iterate:
    >>> subset = []
    >>> for i, data_point in enumerate(dataloader):
    ...     # Say you save a checkpoint on the fourth batch:
    ...     if i == 3:
    ...         _ = checkpointer.save_checkpoint(end_of_epoch = False)
    ...     # So let's save the numbers you would get if you continue
    ...     if i >= 4:
    ...         subset.append(data_point.item())
    >>> # What if instead you had to restart the experiment?
    >>> new_sampler = ReproducibleRandomSampler(dataset)
    >>> new_dataloader = SaveableDataLoader(dataset, sampler = new_sampler,
    ...        num_workers = 3)
    >>> new_checkpointer = Checkpointer(tmpdir, {"dataloader": new_dataloader})
    >>> _ = new_checkpointer.recover_if_possible()
    >>> # You'll get the same random order again:
    >>> new_subset = [data_point.item() for data_point in new_dataloader]
    >>> assert subset == new_subset

    """

    def __init__(self, data_source, seed=563375142, epoch=0, **kwargs):
        if "generator" in kwargs:
            MSG = (
                "Cannot give a separate generator when using "
                + "ReproducibleRandomSampler"
            )
            raise ValueError(MSG)
        super().__init__(data_source, **kwargs)
        self.seed = int(seed)
        self.epoch = epoch
        self.generator = torch.Generator()

    def set_epoch(self, epoch):
        """
        You can also just access self.epoch, but we maintain this interface
        to mirror torch.utils.data.distributed.DistributedSampler
        """
        self.epoch = epoch

    def __iter__(self):
        self.generator.manual_seed(self.seed + self.epoch)
        return super().__iter__()


class ConcatDatasetBatchSampler(Sampler):
    """
    This sampler is built to work with a standard Pytorch ConcatDataset.
    It is used to retrieve elements from the different concatenated datasets placing them in the same batch
    with proportion specified by batch_sizes, e.g 8, 16 means each batch will
    be of 24 elements with the first 8 belonging to the first dataset in ConcatDataset
    object and the last 16 to the second.
    More than two datasets are supported, in that case you need to provide 3 batch
    sizes.

    Note
    ----
    Batched are drawn from the datasets till the one with smallest length is exhausted.
    Thus number of examples in your training epoch is dictated by the dataset
    whose length is the smallest.


    Arguments
    ---------
    samplers : int
        The base seed to use for the random number generator. It is recommended
        to use a value which has a good mix of 0 and 1 bits.
    batch_sizes: list

    epoch : int
        The epoch to start at.

    Example
    -------
    >>> import torch
    >>> from speechbrain.dataio.sampler import ConcatDatasetBatchSampler, ReproducibleRandomSampler
    >>> from speechbrain.dataio.sampler import ReproducibleRandomSampler
    >>> from speechbrain.dataio.dataloader import SaveableDataLoader
    >>> # example "datasets"
    >>> dataset1 = torch.arange(0, 10).unsqueeze(1)
    >>> dataset2 = torch.arange(20, 40).unsqueeze(1)
    >>> tot_dataset = torch.utils.data.ConcatDataset([dataset1, dataset2])
    >>> sampler1 = ReproducibleRandomSampler(dataset1)
    >>> sampler2 = ReproducibleRandomSampler(dataset2)
    >>> tot_sampler = ConcatDatasetBatchSampler([sampler1, sampler2], [2, 4])
    >>> dataloader = SaveableDataLoader(tot_dataset, batch_sampler = tot_sampler,
    ...     num_workers = 3)
    >>> for data_point in dataloader:
    ...      assert len(data_point) == 6
    ...      for i in range(2):
    ...         assert data_point[i] in [x for x in range(0, 10)]
    ...      for i in range(2, 4):
    ...         assert data_point[i] in [x for x in range(10, 40)]
    """

    def __init__(self, samplers, batch_sizes: (tuple, list), epoch=0) -> None:

        if not isinstance(samplers, (list, tuple)):
            raise ValueError(
                "samplers should be a list or tuple of Pytorch Samplers, "
                "but got samplers={}".format(batch_sizes)
            )

        if not isinstance(batch_sizes, (list, tuple)):
            raise ValueError(
                "batch_sizes should be a list or tuple of integers, "
                "but got batch_sizes={}".format(batch_sizes)
            )

        if not len(batch_sizes) == len(samplers):
            raise ValueError(
                "batch_sizes and samplers should be have same length"
            )

        self.batch_sizes = batch_sizes
        self.samplers = samplers
        self.offsets = [0] + np.cumsum(
            [len(x) for x in self.samplers]
        ).tolist()[:-1]

        self.epoch = epoch
        self.set_epoch(self.epoch)

    def _iter_one_dataset(self, c_batch_size, c_sampler, c_offset):
        batch = []
        for idx in c_sampler:
            batch.append(c_offset + idx)
            if len(batch) == c_batch_size:
                yield batch

    def set_epoch(self, epoch):
        """
        You can also just access self.epoch, but we maintain this interface
        to mirror torch.utils.data.distributed.DistributedSampler
        """
        if hasattr(self.samplers[0], "epoch"):
            for s in self.samplers:
                s.set_epoch(epoch)

    def __iter__(self):

        iterators = [iter(i) for i in self.samplers]
        tot_batch = []

        for b_num in range(len(self)):
            for samp_idx in range(len(self.samplers)):
                c_batch = []
                while len(c_batch) < self.batch_sizes[samp_idx]:
                    c_batch.append(
                        self.offsets[samp_idx] + next(iterators[samp_idx])
                    )
                tot_batch.extend(c_batch)
            yield tot_batch
            tot_batch = []

    def __len__(self):

        min_len = float("inf")
        for idx, sampler in enumerate(self.samplers):
            c_len = (
                len(sampler) + self.batch_sizes[idx] - 1
            ) // self.batch_sizes[idx]

            min_len = min(c_len, min_len)
        return min_len


class DynamicBatchSampler(Sampler):
    """
    This BatchSampler batches examples together by grouping them by their length.
    Every example in the batch have approximatively the same length and
    thus padding is minimized.
    This enables faster training on datasets
    where length of examples can vary significantly (e.g Librispeech).
    Inspired by: https://www.tensorflow.org/api_docs/python/tf/data/experimental/bucket_by_sequence_length

    Dynamic batching is performed by specifying a max_batch_length which is the
    upper limit for the sum of the length of examples in a batch:
    e.g. if ex1 has length 4, ex2 length 5 andn if max_batch_length is set to 6
    ex1 and ex2 will be placed, alone, in two distinct batches.

    Length for each example can be obtained in two manners.
    If the input dataset is a DynamicItemDataset it can be obtained by specifying a
    length_func. Default assumes a "duration" entry is in the annotation.
    Length for each example can also be passed to this class upon instantiation
    by specifying a list containing the length for each example and passing it to
    lengths_list.

    Examples are grouped together by defining a set of possible discrete intervals
    (buckets) multiple of a min_bucket_length.
    A bucket_length_multiplier is used to specify the number of possible buckets.
    E.g. if max_batch_length = 32 and min_bucket_length = 10, bucket_length_multiplier = 2
    there will be 3 buckets: [0, 10), [10, 20), [20, 40).
    Decreasing min_bucket_length increases the number of buckets and thus the "tolerance"
    for allowing examples of different length in the same batch.
    Decreasing bucket_length_multiplier has also a similar effect:
    e.g. if max_batch_length = 32 and min_bucket_length = 10, bucket_length_multiplier = 1.5
    the number of buckets increases to 8. With right boundaries: [10 12 14 17 21 25 30 36].
    Thus examples with length less than 10 are all grouped together but more buckets
    are created for longer examples.
    Note that the bucket boundary grows exponentially using the multiplier.

    A common choice would be setting min_bucket_length to approximatively the length
    of your shortest example in the dataset.

    Both arguments thus allow for trading off training speed by having examples
    of almost same length and training stochasticity.

    Example
    -------
    >>> import torch
    >>> import speechbrain as sb
    >>> from speechbrain.dataio.sampler import DynamicBatchSampler
    >>> from speechbrain.dataio.dataset import DynamicItemDataset
    >>> from speechbrain.dataio.dataloader import SaveableDataLoader
    >>> from speechbrain.dataio.batch import PaddedBatch
    >>> import numpy as np
    >>> dataset = {"ex_{}".format(x) : {"wav" :torch.randn(np.random.randint(10, 100))} for x in range(20)}
    >>> dataset = DynamicItemDataset(dataset)
    >>> dataset.set_output_keys(["wav"])
    >>> length_func = lambda x : len(x) # trivial in this example
    >>> bsampler = DynamicBatchSampler(dataset, 20, 10, 1.1, length_func)
    >>> dataloader = SaveableDataLoader(dataset, batch_sampler=bsampler, collate_fn=PaddedBatch)
    >>> for i, b in enumerate(dataloader):
    >>>     data, length = b["wav"]
    >>> assert data.shape[-1] == 76

    Arguments
    ---------
    dataset : torch.utils.data.Dataset
        Pytorch Dataset from which elements will be sampled.
    max_batch_length : int
        Upper limit for the sum of the length of examples in a batch.
        Should be chosen based on your GPU memory.
    min_bucket_length : int
        Minimum length of a bucket. Specifies resolution of buckets and thus this sampler
        stochasticity. A common choice is to set this to length of your
        shortest example.
    bucket_length_multiplier : float
        Multiplier for bucket length, specifies number of buckets from min_bucket_length to
        max_batch_length.
    length_func : callable
        Function used to get length of each example from the dataset.
        This argument can be used only when the dataset is a Speechbrain DynamicItemDataset object.
        Can be anything: e.g. lambda x: x["duration"]*16000 returns number of samples
        if duration key in the annotation is in seconds and the file has 16kHz sampling freq.
    shuffle : bool
        Whether or not shuffle examples between each epoch.
    bucket_boundaries : list
        Overrides bucket_length_multiplier and min_bucket_length by specifying manually
        the buckets right boundaries.
    lengths_list: list
        Overrides length_func by passing a list containing the length of each example
        in the dataset. This argument must be set when the dataset is a plain
        Pytorch Dataset object and not a DynamicItemDataset object as length_func
        cannot be used on Pytorch Datasets.
    epoch : int
        The epoch to start at.
    drop_last : bool
         If ``True``, the sampler will drop the last examples which
         have not been grouped.
    """

    def __init__(
        self,
        dataset,
        max_batch_length: int,
        min_bucket_length: int,
        bucket_length_multiplier: float = 1.1,
        length_func=lambda x: x["duration"],
        shuffle: bool = True,
        bucket_boundaries: List[int] = [],
        lengths_list: List[int] = None,
        seed: int = 42,
        epoch: int = 0,
        drop_last: bool = False,
    ):
        self._dataset = dataset
        self._ex_lengths = {}
        ex_ids = list(self._dataset.data.keys())

        if lengths_list is not None:
            # take length of examples from this argument and bypass length_key
            for indx in range(len(lengths_list)):
                self._ex_lengths[str(indx)] = lengths_list[indx]
        else:
            # use length func
            if not isinstance(dataset, DynamicItemDataset):
                raise NotImplementedError(
                    "Dataset should be a Speechbrain DynamicItemDataset when using length function"
                )
            for indx in range(len(self._dataset)):
                self._ex_lengths[str(indx)] = length_func(
                    self._dataset.data[ex_ids[indx]]
                )

        if bucket_boundaries is not None:
            if not all([x >= 1 for x in bucket_boundaries]):
                raise ValueError(
                    "All elements in bucket boundaries should be >= 1."
                )
            if not len(set(bucket_boundaries)) == len(bucket_boundaries):
                raise ValueError(
                    "Bucket_boundaries should not contain duplicates."
                )

        self._bucket_boundaries = np.array(
            self._get_data_boundaries(
                max_batch_length=max_batch_length,
                bucket_boundaries=bucket_boundaries,
                min_bucket_length=min_bucket_length,
                bucket_length_multiplier=bucket_length_multiplier,
            )
        )

        self._max_batch_length = max_batch_length
        self._shuffle = shuffle
        self._seed = seed
        self._drop_last = drop_last
        # Calculate bucket lengths
        self._bucket_lens = [
            max(1, math.ceil(max_batch_length / self._bucket_boundaries[i]))
            for i in range(len(self._bucket_boundaries))
        ] + [1]
        self._epoch = epoch
        self._generate_batches()

    def _get_data_boundaries(
        self,
        max_batch_length: int,
        bucket_boundaries: List[int],
        min_bucket_length: int,
        bucket_length_multiplier: float,
    ) -> List[int]:
        if not bucket_boundaries:
            if min_bucket_length <= 0:
                raise ValueError(
                    "min_bucket_length must be >0 if no bucket_boundaries set"
                )
            if bucket_length_multiplier < 1.0:
                raise ValueError(
                    "bucket_length_multiplier must be >1.0 if no bucket_boundaries set"
                )
            bucket_boundaries = {min_bucket_length}
            bucket_boundary = float(min_bucket_length)
            while True:
                bucket_boundary *= bucket_length_multiplier
                bucket_boundaries.add(bucket_boundary)
                if bucket_boundary >= max_batch_length:
                    break
        return list(sorted(bucket_boundaries))

    def _generate_batches(self):
        logger.info("DynamicBatchSampler: Generating dynamic batches")

        if self._shuffle:
            # deterministically shuffle based on epoch and seed
            g = torch.Generator()
            g.manual_seed(self._seed + self._epoch)
            sampler = torch.randperm(len(self._dataset), generator=g).tolist()  # type: ignore
        else:
            sampler = range(len(self._dataset))  # type: ignore

        self._batches = []
        bucket_batches = [[] for i in self._bucket_lens]
        bucket_stats = [0 for i in self._bucket_lens]
        for idx in sampler:
            item_len = self._ex_lengths[str(idx)]
            bucket_id = np.searchsorted(self._bucket_boundaries, item_len)
            bucket_batches[bucket_id].append(idx)
            bucket_stats[bucket_id] += 1
            if len(bucket_batches[bucket_id]) >= self._bucket_lens[bucket_id]:
                self._batches.append(bucket_batches[bucket_id])
                bucket_batches[bucket_id] = []
        # Dump remaining batches - we might even want to shuffle those
        if not self._drop_last:
            for batch in bucket_batches:
                if batch:
                    self._batches.append(batch)
        if self._epoch == 0:  # only log at first epoch
            logger.info(
                "DynamicBatchSampler: Created {} batches, {} buckets used.".format(
                    len(self._batches), len(self._bucket_boundaries)
                )
            )
            for i in range(len(self._bucket_lens)):
                logger.info(
                    "DynamicBatchSampler: Bucket {} has {} examples.".format(
                        i, bucket_stats[i]
                    )
                )

    def __iter__(self):
        for batch in self._batches:
            yield batch
        if self._shuffle:  # re-generate batches only if shuffling
            self._generate_batches()

    def set_epoch(self, epoch):
        """
        You can also just access self.epoch, but we maintain this interface
        to mirror torch.utils.data.distributed.DistributedSampler
        """
        self._epoch = epoch
        self._generate_batches()

    def __len__(self):
        return len(self._batches)


# Heavily inspired by Catalyst, which is under Apache 2.0 licence.
# https://github.com/catalyst-team/catalyst/blob/51428d7756e62b9b8ee5379f38e9fd576eeb36e5/catalyst/data/sampler.py#L522
class DistributedSamplerWrapper(DistributedSampler):
    """This wrapper allows using any sampler with Distributed Data Parallel (DDP) correctly.
        Passing blindly the sampler to each DDP process will cause to have access
        within each process to all the data in the dataset instead of only a
        subset of it which is unique to each process.
        This wrapper prevents this and allows to use only a subset of the original
        data for each process.
        It is automatically applied to any sampler in the Brain class when DDP training is used.
    """

    def __init__(self, sampler, *args, **kwargs):
        # DistributedSampler only calls len() on dataset
        # so a sampler is fine to pass there, as well.
        super().__init__(dataset=sampler, *args, **kwargs)
        self.sampler = sampler

    def __iter__(self):
        # It is easiest to use a random access interface to the wrapped
        # sampler's indices, so we just fetch all indices from the wrapped
        # sampler
        sampler_indices = list(self.sampler.__iter__())
        indices_of_indices = super().__iter__()
        # Itemgetter fetches the wrapped sampler indices from the positions
        # pointed to by DistributedSampler
        return iter(itemgetter(*indices_of_indices)(sampler_indices))

    def set_epoch(self, epoch):
        """Pass set_epoch() through to DistributedSampler and the wrapper one"""
        super().set_epoch(epoch)
        if hasattr(self.sampler, "set_epoch"):
            self.sampler.set_epoch(epoch)