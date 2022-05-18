import logging
import warnings
from typing import Any, Dict, Optional, Union

import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
from pytorch_lightning.accelerators import Accelerator

from scvi.dataloaders import DataSplitter, SemiSupervisedDataSplitter
from scvi.model._utils import parse_use_gpu_arg
from scvi.model.base import BaseModelClass
from scvi.train import Trainer

logger = logging.getLogger(__name__)


class MPSAccelerator(Accelerator):
    """Experimental support for MPS, optimized for large-scale machine learning."""

    @staticmethod
    def parse_devices(devices: Any) -> Any:
        # Put parsing logic here how devices can be passed into the Trainer
        # via the `devices` argument
        return devices

    @staticmethod
    def get_parallel_devices(devices: Any) -> Any:
        # Here, convert the device indices to actual device objects
        return [torch.device("mps")]

    @staticmethod
    def auto_device_count() -> int:
        # Return a value for auto-device selection when `Trainer(devices="auto")`
        return 1

    @staticmethod
    def is_available() -> bool:
        try:
            torch.ones(5, 5, device=torch.device("mps"))
        except AssertionError:
            return False
        return True

    def get_device_stats(self, device: Union[str, torch.device]) -> Dict[str, Any]:
        # Return optional device statistics for loggers
        return {}

    @classmethod
    def register_accelerators(cls, accelerator_registry):
        accelerator_registry.register(
            "mps",
            cls,
            description="MPS Accelerator - optimized for Apple Silicon.",
        )


class TrainRunner:
    """
    TrainRunner calls Trainer.fit() and handles pre and post training procedures.

    Parameters
    ----------
    model
        model to train
    training_plan
        initialized TrainingPlan
    data_splitter
        initialized :class:`~scvi.dataloaders.SemiSupervisedDataSplitter` or
        :class:`~scvi.dataloaders.DataSplitter`
    max_epochs
        max_epochs to train for
    use_gpu
        Use default GPU if available (if None or True), or index of GPU to use (if int),
        or name of GPU (if str, e.g., `'cuda:0'`), or use CPU (if False).
    trainer_kwargs
        Extra kwargs for :class:`~scvi.train.Trainer`

    Examples
    --------
    >>> # Following code should be within a subclass of BaseModelClass
    >>> data_splitter = DataSplitter(self.adata)
    >>> training_plan = TrainingPlan(self.module, len(data_splitter.train_idx))
    >>> runner = TrainRunner(
    >>>     self,
    >>>     training_plan=trianing_plan,
    >>>     data_splitter=data_splitter,
    >>>     max_epochs=max_epochs)
    >>> runner()
    """

    def __init__(
        self,
        model: BaseModelClass,
        training_plan: pl.LightningModule,
        data_splitter: Union[SemiSupervisedDataSplitter, DataSplitter],
        max_epochs: int,
        use_gpu: Optional[Union[str, int, bool]] = None,
        **trainer_kwargs,
    ):
        self.training_plan = training_plan
        self.data_splitter = data_splitter
        self.model = model
        gpus, device = parse_use_gpu_arg(use_gpu)
        accelerator = MPSAccelerator()
        if accelerator.is_available() and use_gpu is not False:
            gpus = None
            device = torch.device("mps")
            trainer_kwargs.update(dict(accelerator=accelerator, devices=1))
            logger.info("Using Apple Silicon accelerator.")
        self.gpus = gpus
        self.device = device
        self.trainer = Trainer(max_epochs=max_epochs, gpus=gpus, **trainer_kwargs)

    def __call__(self):
        if hasattr(self.data_splitter, "n_train"):
            self.training_plan.n_obs_training = self.data_splitter.n_train
        if hasattr(self.data_splitter, "n_val"):
            self.training_plan.n_obs_validation = self.data_splitter.n_val

        self.trainer.fit(self.training_plan, self.data_splitter)
        self._update_history()

        # data splitter only gets these attrs after fit
        self.model.train_indices = self.data_splitter.train_idx
        self.model.test_indices = self.data_splitter.test_idx
        self.model.validation_indices = self.data_splitter.val_idx

        self.model.module.eval()
        self.model.is_trained_ = True
        self.model.to_device(self.device)
        self.model.trainer = self.trainer

    def _update_history(self):
        # model is being further trained
        # this was set to true during first training session
        if self.model.is_trained_ is True:
            # if not using the default logger (e.g., tensorboard)
            if not isinstance(self.model.history_, dict):
                warnings.warn(
                    "Training history cannot be updated. Logger can be accessed from model.trainer.logger"
                )
                return
            else:
                new_history = self.trainer.logger.history
                for key, val in self.model.history_.items():
                    # e.g., no validation loss due to training params
                    if key not in new_history:
                        continue
                    prev_len = len(val)
                    new_len = len(new_history[key])
                    index = np.arange(prev_len, prev_len + new_len)
                    new_history[key].index = index
                    self.model.history_[key] = pd.concat(
                        [
                            val,
                            new_history[key],
                        ]
                    )
                    self.model.history_[key].index.name = val.index.name
        else:
            # set history_ attribute if it exists
            # other pytorch lightning loggers might not have history attr
            try:
                self.model.history_ = self.trainer.logger.history
            except AttributeError:
                self.history_ = None
