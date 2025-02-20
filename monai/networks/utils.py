# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Utilities and types for defining networks, these depend on PyTorch.
"""
import re
import warnings
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Union

import torch
import torch.nn as nn

from monai.utils.deprecate_utils import deprecated_arg
from monai.utils.misc import ensure_tuple, set_determinism
from monai.utils.module import PT_BEFORE_1_7

__all__ = [
    "one_hot",
    "slice_channels",
    "predict_segmentation",
    "normalize_transform",
    "to_norm_affine",
    "normal_init",
    "icnr_init",
    "pixelshuffle",
    "eval_mode",
    "train_mode",
    "copy_model_state",
    "convert_to_torchscript",
]


def one_hot(labels: torch.Tensor, num_classes: int, dtype: torch.dtype = torch.float, dim: int = 1) -> torch.Tensor:
    """
    For every value v in `labels`, the value in the output will be either 1 or 0. Each vector along the `dim`-th
    dimension has the "one-hot" format, i.e., it has a total length of `num_classes`,
    with a one and `num_class-1` zeros.
    Note that this will include the background label, thus a binary mask should be treated as having two classes.

    Args:
        labels: input tensor of integers to be converted into the 'one-hot' format. Internally `labels` will be
            converted into integers `labels.long()`.
        num_classes: number of output channels, the corresponding length of `labels[dim]` will be converted to
            `num_classes` from `1`.
        dtype: the data type of the output one_hot label.
        dim: the dimension to be converted to `num_classes` channels from `1` channel, should be non-negative number.

    Example:

    For a tensor `labels` of dimensions [B]1[spatial_dims], return a tensor of dimensions `[B]N[spatial_dims]`
    when `num_classes=N` number of classes and `dim=1`.

    .. code-block:: python

        from monai.networks.utils import one_hot
        import torch

        a = torch.randint(0, 2, size=(1, 2, 2, 2))
        out = one_hot(a, num_classes=2, dim=0)
        print(out.shape)  # torch.Size([2, 2, 2, 2])

        a = torch.randint(0, 2, size=(2, 1, 2, 2, 2))
        out = one_hot(a, num_classes=2, dim=1)
        print(out.shape)  # torch.Size([2, 2, 2, 2, 2])

    """

    # if `dim` is bigger, add singleton dim at the end
    if labels.ndim < dim + 1:
        shape = list(labels.shape) + [1] * (dim + 1 - len(labels.shape))
        labels = torch.reshape(labels, shape)

    sh = list(labels.shape)

    if sh[dim] != 1:
        raise AssertionError("labels should have a channel with length equal to one.")

    sh[dim] = num_classes

    o = torch.zeros(size=sh, dtype=dtype, device=labels.device)
    labels = o.scatter_(dim=dim, index=labels.long(), value=1)

    return labels


def slice_channels(tensor: torch.Tensor, *slicevals: Optional[int]) -> torch.Tensor:
    slices = [slice(None)] * len(tensor.shape)
    slices[1] = slice(*slicevals)

    return tensor[slices]


def predict_segmentation(logits: torch.Tensor, mutually_exclusive: bool = False, threshold: float = 0.0) -> Any:
    """
    Given the logits from a network, computing the segmentation by thresholding all values above 0
    if multi-labels task, computing the `argmax` along the channel axis if multi-classes task,
    logits has shape `BCHW[D]`.

    Args:
        logits: raw data of model output.
        mutually_exclusive: if True, `logits` will be converted into a binary matrix using
            a combination of argmax, which is suitable for multi-classes task. Defaults to False.
        threshold: thresholding the prediction values if multi-labels task.
    """
    if not mutually_exclusive:
        return (logits >= threshold).int()
    if logits.shape[1] == 1:
        warnings.warn("single channel prediction, `mutually_exclusive=True` ignored, use threshold instead.")
        return (logits >= threshold).int()
    return logits.argmax(1, keepdim=True)


def normalize_transform(
    shape: Sequence[int],
    device: Optional[torch.device] = None,
    dtype: Optional[torch.dtype] = None,
    align_corners: bool = False,
) -> torch.Tensor:
    """
    Compute an affine matrix according to the input shape.
    The transform normalizes the homogeneous image coordinates to the
    range of `[-1, 1]`.

    Args:
        shape: input spatial shape
        device: device on which the returned affine will be allocated.
        dtype: data type of the returned affine
        align_corners: if True, consider -1 and 1 to refer to the centers of the
            corner pixels rather than the image corners.
            See also: https://pytorch.org/docs/stable/nn.functional.html#torch.nn.functional.grid_sample
    """
    norm = torch.tensor(shape, dtype=torch.float64, device=device)  # no in-place change
    if align_corners:
        norm[norm <= 1.0] = 2.0
        norm = 2.0 / (norm - 1.0)
        norm = torch.diag(torch.cat((norm, torch.ones((1,), dtype=torch.float64, device=device))))
        norm[:-1, -1] = -1.0
    else:
        norm[norm <= 0.0] = 2.0
        norm = 2.0 / norm
        norm = torch.diag(torch.cat((norm, torch.ones((1,), dtype=torch.float64, device=device))))
        norm[:-1, -1] = 1.0 / torch.tensor(shape, dtype=torch.float64, device=device) - 1.0
    norm = norm.unsqueeze(0).to(dtype=dtype)
    norm.requires_grad = False
    return norm


def to_norm_affine(
    affine: torch.Tensor, src_size: Sequence[int], dst_size: Sequence[int], align_corners: bool = False
) -> torch.Tensor:
    """
    Given ``affine`` defined for coordinates in the pixel space, compute the corresponding affine
    for the normalized coordinates.

    Args:
        affine: Nxdxd batched square matrix
        src_size: source image spatial shape
        dst_size: target image spatial shape
        align_corners: if True, consider -1 and 1 to refer to the centers of the
            corner pixels rather than the image corners.
            See also: https://pytorch.org/docs/stable/nn.functional.html#torch.nn.functional.grid_sample

    Raises:
        TypeError: When ``affine`` is not a ``torch.Tensor``.
        ValueError: When ``affine`` is not Nxdxd.
        ValueError: When ``src_size`` or ``dst_size`` dimensions differ from ``affine``.

    """
    if not isinstance(affine, torch.Tensor):
        raise TypeError(f"affine must be a torch.Tensor but is {type(affine).__name__}.")
    if affine.ndimension() != 3 or affine.shape[1] != affine.shape[2]:
        raise ValueError(f"affine must be Nxdxd, got {tuple(affine.shape)}.")
    sr = affine.shape[1] - 1
    if sr != len(src_size) or sr != len(dst_size):
        raise ValueError(f"affine suggests {sr}D, got src={len(src_size)}D, dst={len(dst_size)}D.")

    src_xform = normalize_transform(src_size, affine.device, affine.dtype, align_corners)
    dst_xform = normalize_transform(dst_size, affine.device, affine.dtype, align_corners)
    return src_xform @ affine @ torch.inverse(dst_xform)


def normal_init(
    m, std: float = 0.02, normal_func: Callable[[torch.Tensor, float, float], Any] = torch.nn.init.normal_
) -> None:
    """
    Initialize the weight and bias tensors of `m' and its submodules to values from a normal distribution with a
    stddev of `std'. Weight tensors of convolution and linear modules are initialized with a mean of 0, batch
    norm modules with a mean of 1. The callable `normal_func', used to assign values, should have the same arguments
    as its default normal_(). This can be used with `nn.Module.apply` to visit submodules of a network.
    """
    cname = m.__class__.__name__

    if getattr(m, "weight", None) is not None and (cname.find("Conv") != -1 or cname.find("Linear") != -1):
        normal_func(m.weight.data, 0.0, std)
        if getattr(m, "bias", None) is not None:
            nn.init.constant_(m.bias.data, 0.0)

    elif cname.find("BatchNorm") != -1:
        normal_func(m.weight.data, 1.0, std)
        nn.init.constant_(m.bias.data, 0)


def icnr_init(conv, upsample_factor, init=nn.init.kaiming_normal_):
    """
    ICNR initialization for 2D/3D kernels adapted from Aitken et al.,2017 , "Checkerboard artifact free
    sub-pixel convolution".
    """
    out_channels, in_channels, *dims = conv.weight.shape
    scale_factor = upsample_factor ** len(dims)

    oc2 = int(out_channels / scale_factor)

    kernel = torch.zeros([oc2, in_channels] + dims)
    kernel = init(kernel)
    kernel = kernel.transpose(0, 1)
    kernel = kernel.reshape(oc2, in_channels, -1)
    kernel = kernel.repeat(1, 1, scale_factor)
    kernel = kernel.reshape([in_channels, out_channels] + dims)
    kernel = kernel.transpose(0, 1)
    conv.weight.data.copy_(kernel)


@deprecated_arg(
    name="dimensions", new_name="spatial_dims", since="0.6", msg_suffix="Please use `spatial_dims` instead."
)
def pixelshuffle(
    x: torch.Tensor, spatial_dims: int, scale_factor: int, dimensions: Optional[int] = None
) -> torch.Tensor:
    """
    Apply pixel shuffle to the tensor `x` with spatial dimensions `spatial_dims` and scaling factor `scale_factor`.

    See: Shi et al., 2016, "Real-Time Single Image and Video Super-Resolution
    Using a nEfficient Sub-Pixel Convolutional Neural Network."

    See: Aitken et al., 2017, "Checkerboard artifact free sub-pixel convolution".

    Args:
        x: Input tensor
        spatial_dims: number of spatial dimensions, typically 2 or 3 for 2D or 3D
        scale_factor: factor to rescale the spatial dimensions by, must be >=1

    .. deprecated:: 0.6.0
        ``dimensions`` is deprecated, use ``spatial_dims`` instead.

    Returns:
        Reshuffled version of `x`.

    Raises:
        ValueError: When input channels of `x` are not divisible by (scale_factor ** spatial_dims)
    """
    if dimensions is not None:
        spatial_dims = dimensions
    dim, factor = spatial_dims, scale_factor
    input_size = list(x.size())
    batch_size, channels = input_size[:2]
    scale_divisor = factor ** dim

    if channels % scale_divisor != 0:
        raise ValueError(
            f"Number of input channels ({channels}) must be evenly "
            f"divisible by scale_factor ** dimensions ({factor}**{dim}={scale_divisor})."
        )

    org_channels = channels // scale_divisor
    output_size = [batch_size, org_channels] + [d * factor for d in input_size[2:]]

    indices = tuple(range(2, 2 + 2 * dim))
    indices_factor, indices_dim = indices[:dim], indices[dim:]
    permute_indices = (0, 1) + sum(zip(indices_dim, indices_factor), ())

    x = x.reshape(batch_size, org_channels, *([factor] * dim + input_size[2:]))
    x = x.permute(permute_indices).reshape(output_size)
    return x


@contextmanager
def eval_mode(*nets: nn.Module):
    """
    Set network(s) to eval mode and then return to original state at the end.

    Args:
        nets: Input network(s)

    Examples

    .. code-block:: python

        t=torch.rand(1,1,16,16)
        p=torch.nn.Conv2d(1,1,3)
        print(p.training)  # True
        with eval_mode(p):
            print(p.training)  # False
            print(p(t).sum().backward())  # will correctly raise an exception as gradients are calculated
    """

    # Get original state of network(s)
    training = [n for n in nets if n.training]

    try:
        # set to eval mode
        with torch.no_grad():
            yield [n.eval() for n in nets]
    finally:
        # Return required networks to training
        for n in training:
            n.train()


@contextmanager
def train_mode(*nets: nn.Module):
    """
    Set network(s) to train mode and then return to original state at the end.

    Args:
        nets: Input network(s)

    Examples

    .. code-block:: python

        t=torch.rand(1,1,16,16)
        p=torch.nn.Conv2d(1,1,3)
        p.eval()
        print(p.training)  # False
        with train_mode(p):
            print(p.training)  # True
            print(p(t).sum().backward())  # No exception
    """

    # Get original state of network(s)
    eval_list = [n for n in nets if not n.training]

    try:
        # set to train mode
        with torch.set_grad_enabled(True):
            yield [n.train() for n in nets]
    finally:
        # Return required networks to eval_list
        for n in eval_list:
            n.eval()


def copy_model_state(
    dst: Union[torch.nn.Module, Mapping],
    src: Union[torch.nn.Module, Mapping],
    dst_prefix="",
    mapping=None,
    exclude_vars=None,
    inplace=True,
):
    """
    Compute a module state_dict, of which the keys are the same as `dst`. The values of `dst` are overwritten
    by the ones from `src` whenever their keys match. The method provides additional `dst_prefix` for
    the `dst` key when matching them. `mapping` can be a `{"src_key": "dst_key"}` dict, indicating
    `dst[dst_prefix + dst_key] = src[src_key]`.
    This function is mainly to return a model state dict
    for loading the `src` model state into the `dst` model, `src` and `dst` can have different dict keys, but
    their corresponding values normally have the same shape.

    Args:
        dst: a pytorch module or state dict to be updated.
        src: a pytorch module or state dist used to get the values used for the update.
        dst_prefix: `dst` key prefix, so that `dst[dst_prefix + src_key]`
            will be assigned to the value of `src[src_key]`.
        mapping: a `{"src_key": "dst_key"}` dict, indicating that `dst[dst_prefix + dst_key]`
            to be assigned to the value of `src[src_key]`.
        exclude_vars: a regular expression to match the `dst` variable names,
            so that their values are not overwritten by `src`.
        inplace: whether to set the `dst` module with the updated `state_dict` via `load_state_dict`.
            This option is only available when `dst` is a `torch.nn.Module`.

    Examples:
        .. code-block:: python

            from monai.networks.nets import BasicUNet
            from monai.networks.utils import copy_model_state

            model_a = BasicUNet(in_channels=1, out_channels=4)
            model_b = BasicUNet(in_channels=1, out_channels=2)
            model_a_b, changed, unchanged = copy_model_state(
                model_a, model_b, exclude_vars="conv_0.conv_0", inplace=False)
            # dst model updated: 76 of 82 variables.
            model_a.load_state_dict(model_a_b)
            # <All keys matched successfully>

    Returns: an OrderedDict of the updated `dst` state, the changed, and unchanged keys.
    """

    if isinstance(src, (nn.DataParallel, nn.parallel.DistributedDataParallel)):
        src = src.module
    if isinstance(dst, (nn.DataParallel, nn.parallel.DistributedDataParallel)):
        dst = dst.module
    src_dict = src.state_dict() if isinstance(src, torch.nn.Module) else src
    dst_dict = dst.state_dict() if isinstance(dst, torch.nn.Module) else dst
    dst_dict = OrderedDict(dst_dict)

    to_skip = {s_key for s_key in src_dict if exclude_vars and re.compile(exclude_vars).search(s_key)}

    # update dst with items from src
    all_keys, updated_keys = list(dst_dict), list()
    for s, val in src_dict.items():
        dst_key = f"{dst_prefix}{s}"
        if dst_key in dst_dict and dst_key not in to_skip and dst_dict[dst_key].shape == val.shape:
            dst_dict[dst_key] = val
            updated_keys.append(dst_key)
    for s in mapping if mapping else {}:
        dst_key = f"{dst_prefix}{mapping[s]}"
        if dst_key in dst_dict and dst_key not in to_skip:
            if dst_dict[dst_key].shape != src_dict[s].shape:
                warnings.warn(f"Param. shape changed from {dst_dict[dst_key].shape} to {src_dict[s].shape}.")
            dst_dict[dst_key] = src_dict[s]
            updated_keys.append(dst_key)

    updated_keys = sorted(set(updated_keys))
    unchanged_keys = sorted(set(all_keys).difference(updated_keys))
    print(f"'dst' model updated: {len(updated_keys)} of {len(dst_dict)} variables.")
    if inplace and isinstance(dst, torch.nn.Module):
        dst.load_state_dict(dst_dict)
    return dst_dict, updated_keys, unchanged_keys


def convert_to_torchscript(
    model: nn.Module,
    filename_or_obj: Optional[Any] = None,
    extra_files: Optional[Dict] = None,
    verify: bool = False,
    inputs: Optional[Sequence[Any]] = None,
    device: Optional[torch.device] = None,
    rtol: float = 1e-4,
    atol: float = 0.0,
    **kwargs,
):
    """
    Utility to convert a model into TorchScript model and save to file,
    with optional input / output data verification.

    Args:
        model: source PyTorch model to save.
        filename_or_obj: if not None, specify a file-like object (has to implement write and flush)
            or a string containing a file path name to save the TorchScript model.
        extra_files: map from filename to contents which will be stored as part of the save model file.
            works for PyTorch 1.7 or later.
            for more details: https://pytorch.org/docs/stable/generated/torch.jit.save.html.
        verify: whether to verify the input and output of TorchScript model.
            if `filename_or_obj` is not None, load the saved TorchScript model and verify.
        inputs: input test data to verify model, should be a sequence of data, every item maps to a argument
            of `model()` function.
        device: target device to verify the model, if None, use CUDA if available.
        rtol: the relative tolerance when comparing the outputs of PyTorch model and TorchScript model.
        atol: the absolute tolerance when comparing the outputs of PyTorch model and TorchScript model.

    """
    model.eval()
    with torch.no_grad():
        script_module = torch.jit.script(model)
        if filename_or_obj is not None:
            if PT_BEFORE_1_7:
                torch.jit.save(m=script_module, f=filename_or_obj)
            else:
                torch.jit.save(m=script_module, f=filename_or_obj, _extra_files=extra_files)

    if verify:
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if inputs is None:
            raise ValueError("missing input data for verification.")

        inputs = [i.to(device) if isinstance(i, torch.Tensor) else i for i in inputs]
        ts_model = torch.jit.load(filename_or_obj) if filename_or_obj is not None else script_module
        ts_model.eval().to(device)
        model = model.to(device)

        with torch.no_grad():
            set_determinism(seed=0)
            torch_out = ensure_tuple(model(*inputs))
            set_determinism(seed=0)
            torchscript_out = ensure_tuple(ts_model(*inputs))
            set_determinism(seed=None)
        # compare TorchScript and PyTorch results
        for r1, r2 in zip(torch_out, torchscript_out):
            if isinstance(r1, torch.Tensor) or isinstance(r2, torch.Tensor):
                torch.testing.assert_allclose(r1, r2, rtol=rtol, atol=atol)

    return script_module
