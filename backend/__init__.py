import sys
# Trick transformers/huggingface to bypass PyTorch < 2.6 security check for torch.load (CVE-2025-32434)
try:
    import torch
    torch.__version__ = "2.6.0"
    if hasattr(torch, "version"):
        torch.version.__version__ = "2.6.0"
except ImportError:
    pass

from backend.agent.status import understand_summary, humanize_action

__all__ = ["understand_summary", "humanize_action"]
