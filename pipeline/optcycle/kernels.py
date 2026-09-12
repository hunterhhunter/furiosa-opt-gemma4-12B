from dataclasses import dataclass


class KernelError(ValueError):
    """Raised when an unsupported kernel is requested."""


@dataclass(frozen=True)
class KernelSpec:
    name: str
    rust_path: str


KERNELS = {
    "sliding_project_qkv": KernelSpec(
        "sliding_project_qkv", "ops::sliding_project_qkv"
    ),
    "sliding_attention_output": KernelSpec(
        "sliding_attention_output", "ops::sliding_attention_output"
    ),
    "decoder_feedforward": KernelSpec(
        "decoder_feedforward", "ops::decoder_feedforward"
    ),
}


def get_kernel(name: str) -> KernelSpec:
    try:
        return KERNELS[name]
    except KeyError as error:
        supported = ", ".join(sorted(KERNELS))
        raise KernelError(f"unsupported kernel {name!r}; choose one of: {supported}") from error
