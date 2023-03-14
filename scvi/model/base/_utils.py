import logging
import os
import pickle
import warnings
from collections.abc import Iterable as IterableClass
from typing import List, Literal, Optional, Tuple, Union

import anndata
import mudata
import numpy as np
import pandas as pd
import torch
from anndata import AnnData, read

from scvi.data._constants import _SETUP_METHOD_NAME
from scvi.data._download import _download
from scvi.module.base._decorators import _move_data_to_device
from scvi.utils import track

from ._differential import DifferentialComputation

logger = logging.getLogger(__name__)


def subset_distribution(my_distribution, index, dim=0):
    """Utility function to subset the parameter of a Pytorch distribution."""
    return my_distribution.__class__(
        **{
            name: torch.index_select(
                getattr(my_distribution, name), dim=dim, index=index
            )
            for name in my_distribution.arg_constraints.keys()
        }
    )


def move_distribution(my_distribution, device):
    """Utility function to move the parameter of a Pytorch distribution to a specific device."""
    new_params = {
        name: _move_data_to_device(getattr(my_distribution, name), device)
        for name in my_distribution.arg_constraints.keys()
    }
    return my_distribution.__class__(**new_params)


class DistributionsConcatenator:
    """Utility class to concatenate Pytorch distributions and move them to cpu."""

    def __init__(self):
        self.storage = {}

    def add_distributions(self, forward_outputs):
        """Add a dictionary of distributions to the concatenator."""
        for key, potential_distribution in forward_outputs.items():
            if isinstance(potential_distribution, torch.distributions.Distribution):
                if key not in self.storage:
                    params = {
                        name: []
                        for name in potential_distribution.arg_constraints.keys()
                    }
                    self.storage[key] = dict(
                        cls=potential_distribution.__class__,
                        **params,
                    )
                new_params = {
                    name: getattr(potential_distribution, name).cpu()
                    for name in potential_distribution.arg_constraints.keys()
                }
                for param_name, param in new_params.items():
                    self.storage[key][param_name].append(param)

    @staticmethod
    def _find_concat_dim(my_list):
        ndims = my_list[0].ndim
        if ndims == 2:
            return 0
        elif ndims == 3:
            return 1
        else:
            raise ValueError("Only 2D and 3D tensors are supported.")

    def get_concatenated_distributions(self):
        """Returns concatenated distributions."""
        dists = {}
        for dist_name, dist_props in self.storage.items():
            dist_cls = dist_props.pop("cls")
            concat_params = {
                key: torch.cat(value, dim=self._find_concat_dim(value))
                for key, value in dist_props.items()
            }
            dists[dist_name] = dist_cls(**concat_params)
        return dists


def _load_legacy_saved_files(
    dir_path: str,
    file_name_prefix: str,
    load_adata: bool,
) -> Tuple[dict, np.ndarray, dict, Optional[AnnData]]:
    model_path = os.path.join(dir_path, f"{file_name_prefix}model_params.pt")
    var_names_path = os.path.join(dir_path, f"{file_name_prefix}var_names.csv")
    setup_dict_path = os.path.join(dir_path, f"{file_name_prefix}attr.pkl")

    model_state_dict = torch.load(model_path, map_location="cpu")

    var_names = np.genfromtxt(var_names_path, delimiter=",", dtype=str)

    with open(setup_dict_path, "rb") as handle:
        attr_dict = pickle.load(handle)

    if load_adata:
        adata_path = os.path.join(dir_path, f"{file_name_prefix}adata.h5ad")
        if os.path.exists(adata_path):
            adata = read(adata_path)
        elif not os.path.exists(adata_path):
            raise ValueError(
                "Save path contains no saved anndata and no adata was passed."
            )
    else:
        adata = None

    return model_state_dict, var_names, attr_dict, adata


def _load_saved_files(
    dir_path: str,
    load_adata: bool,
    prefix: Optional[str] = None,
    map_location: Optional[Literal["cpu", "cuda"]] = None,
    backup_url: Optional[str] = None,
) -> Tuple[dict, np.ndarray, dict, AnnData]:
    """Helper to load saved files."""
    file_name_prefix = prefix or ""

    model_file_name = f"{file_name_prefix}model.pt"
    model_path = os.path.join(dir_path, model_file_name)
    try:
        _download(backup_url, dir_path, model_file_name)
        model = torch.load(model_path, map_location=map_location)
    except FileNotFoundError as exc:
        raise ValueError(
            f"Failed to load model file at {model_path}. "
            "If attempting to load a saved model from <v0.15.0, please use the util function "
            "`convert_legacy_save` to convert to an updated format."
        ) from exc

    model_state_dict = model["model_state_dict"]
    var_names = model["var_names"]
    attr_dict = model["attr_dict"]

    if load_adata:
        is_mudata = attr_dict["registry_"].get(_SETUP_METHOD_NAME) == "setup_mudata"
        file_suffix = "adata.h5ad" if is_mudata is False else "mdata.h5mu"
        adata_path = os.path.join(dir_path, f"{file_name_prefix}{file_suffix}")
        if os.path.exists(adata_path):
            if is_mudata:
                adata = mudata.read(adata_path)
            else:
                adata = anndata.read(adata_path)
        else:
            raise ValueError(
                "Save path contains no saved anndata and no adata was passed."
            )
    else:
        adata = None

    return attr_dict, var_names, model_state_dict, adata


def _initialize_model(cls, adata, attr_dict):
    """Helper to initialize a model."""
    if "init_params_" not in attr_dict.keys():
        raise ValueError(
            "No init_params_ were saved by the model. Check out the "
            "developers guide if creating custom models."
        )
    # get the parameters for the class init signature
    init_params = attr_dict.pop("init_params_")

    # new saving and loading, enable backwards compatibility
    if "non_kwargs" in init_params.keys():
        # grab all the parameters except for kwargs (is a dict)
        non_kwargs = init_params["non_kwargs"]
        kwargs = init_params["kwargs"]

        # expand out kwargs
        kwargs = {k: v for (i, j) in kwargs.items() for (k, v) in j.items()}
    else:
        # grab all the parameters except for kwargs (is a dict)
        non_kwargs = {k: v for k, v in init_params.items() if not isinstance(v, dict)}
        kwargs = {k: v for k, v in init_params.items() if isinstance(v, dict)}
        kwargs = {k: v for (i, j) in kwargs.items() for (k, v) in j.items()}
        non_kwargs.pop("use_cuda")

    # backwards compat for scANVI
    if "unlabeled_category" in non_kwargs.keys():
        non_kwargs.pop("unlabeled_category")
    if "pretrained_model" in non_kwargs.keys():
        non_kwargs.pop("pretrained_model")

    model = cls(adata, **non_kwargs, **kwargs)
    for attr, val in attr_dict.items():
        setattr(model, attr, val)

    return model


def _validate_var_names(adata, source_var_names):
    user_var_names = adata.var_names.astype(str)
    if not np.array_equal(source_var_names, user_var_names):
        warnings.warn(
            "var_names for adata passed in does not match var_names of "
            "adata used to train the model. For valid results, the vars "
            "need to be the same and in the same order as the adata used to train the model."
        )


def _prepare_obs(
    idx1: Union[List[bool], np.ndarray, str],
    idx2: Union[List[bool], np.ndarray, str],
    adata: anndata.AnnData,
):
    """Construct an array used for masking.

    Given population identifiers `idx1` and potentially `idx2`,
    this function creates an array `obs_col` that identifies both populations
    for observations contained in `adata`.
    In particular, `obs_col` will take values `group1` (resp. `group2`)
    for `idx1` (resp `idx2`).

    Parameters
    ----------
    idx1
        Can be of three types. First, it can corresponds to a boolean mask that
        has the same shape as adata. It can also corresponds to a list of indices.
        Last, it can correspond to string query of adata.obs columns.
    idx2
        Same as above
    adata
        Anndata
    """

    def ravel_idx(my_idx, obs_df):
        return (
            obs_df.index.isin(obs_df.query(my_idx).index)
            if isinstance(my_idx, str)
            else np.asarray(my_idx).ravel()
        )

    obs_df = adata.obs
    idx1 = ravel_idx(idx1, obs_df)
    g1_key = "one"
    obs_col = np.array(["None"] * adata.shape[0], dtype=str)
    obs_col[idx1] = g1_key
    group1 = [g1_key]
    group2 = None if idx2 is None else "two"
    if idx2 is not None:
        idx2 = ravel_idx(idx2, obs_df)
        obs_col[idx2] = group2
    if (obs_col[idx1].shape[0] == 0) or (obs_col[idx2].shape[0] == 0):
        raise ValueError("One of idx1 or idx2 has size zero.")
    return obs_col, group1, group2


def _de_core(
    adata_manager,
    model_fn,
    representation_fn,
    groupby,
    group1,
    group2,
    idx1,
    idx2,
    all_stats,
    all_stats_fn,
    col_names,
    mode,
    batchid1,
    batchid2,
    delta,
    batch_correction,
    fdr,
    silent,
    **kwargs,
):
    """Internal function for DE interface."""
    adata = adata_manager.adata
    if group1 is None and idx1 is None:
        group1 = adata.obs[groupby].astype("category").cat.categories.tolist()
        if len(group1) == 1:
            raise ValueError(
                "Only a single group in the data. Can't run DE on a single group."
            )

    if not isinstance(group1, IterableClass) or isinstance(group1, str):
        group1 = [group1]

    # make a temp obs key using indices
    temp_key = None
    if idx1 is not None:
        obs_col, group1, group2 = _prepare_obs(idx1, idx2, adata)
        temp_key = "_scvi_temp_de"
        adata.obs[temp_key] = obs_col
        groupby = temp_key

    df_results = []
    dc = DifferentialComputation(model_fn, representation_fn, adata_manager)
    for g1 in track(
        group1,
        description="DE...",
        disable=silent,
    ):
        cell_idx1 = (adata.obs[groupby] == g1).to_numpy().ravel()
        if group2 is None:
            cell_idx2 = ~cell_idx1
        else:
            cell_idx2 = (adata.obs[groupby] == group2).to_numpy().ravel()

        all_info = dc.get_bayes_factors(
            cell_idx1,
            cell_idx2,
            mode=mode,
            delta=delta,
            batchid1=batchid1,
            batchid2=batchid2,
            use_observed_batches=not batch_correction,
            **kwargs,
        )

        if all_stats is True:
            genes_properties_dict = all_stats_fn(adata_manager, cell_idx1, cell_idx2)
            all_info = {**all_info, **genes_properties_dict}

        res = pd.DataFrame(all_info, index=col_names)
        sort_key = "proba_de" if mode == "change" else "bayes_factor"
        res = res.sort_values(by=sort_key, ascending=False)
        if mode == "change":
            res[f"is_de_fdr_{fdr}"] = _fdr_de_prediction(res["proba_de"], fdr=fdr)
        if idx1 is None:
            g2 = "Rest" if group2 is None else group2
            res["comparison"] = f"{g1} vs {g2}"
            res["group1"] = g1
            res["group2"] = g2
        df_results.append(res)

    if temp_key is not None:
        del adata.obs[temp_key]

    result = pd.concat(df_results, axis=0)

    return result


def _fdr_de_prediction(posterior_probas: pd.Series, fdr: float = 0.05) -> pd.Series:
    """Compute posterior expected FDR and tag features as DE."""
    if not posterior_probas.ndim == 1:
        raise ValueError("posterior_probas should be 1-dimensional")
    original_index = posterior_probas.index
    sorted_pgs = posterior_probas.sort_values(ascending=False)
    cumulative_fdr = (1.0 - sorted_pgs).cumsum() / (1.0 + np.arange(len(sorted_pgs)))
    d = (cumulative_fdr <= fdr).sum()
    is_pred_de = pd.Series(
        np.zeros_like(cumulative_fdr).astype(bool), index=sorted_pgs.index
    )
    is_pred_de.iloc[:d] = True
    is_pred_de = is_pred_de.loc[original_index]
    return is_pred_de
