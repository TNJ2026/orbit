"""Thin, stable Runtime Kernel entry point."""

from .kernel_families import RuntimeKernel as _RuntimeKernel


class RuntimeKernel(_RuntimeKernel):
    """The only Command/UoW/Receipt entry point.

    Transactional command families are deliberately implementation details in
    :mod:`kernel_families`; callers depend only on this façade.
    """


__all__ = ["RuntimeKernel"]

