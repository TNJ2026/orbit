"""Recursive Secret redaction for logs, errors, prompts and metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Iterable


class Redactor:
    def __init__(self, secret_values: Iterable[str], replacement: str = "[REDACTED]") -> None:
        self.secrets=tuple(sorted((value for value in secret_values if value),key=len,reverse=True));self.replacement=replacement
    def redact(self,value:Any)->Any:
        if isinstance(value,str):
            for secret in self.secrets:value=value.replace(secret,self.replacement)
            return value
        if isinstance(value,bytes):
            raw=value
            for secret in self.secrets:raw=raw.replace(secret.encode(),self.replacement.encode())
            return raw
        if isinstance(value,Mapping):return {str(key):self.redact(item) for key,item in value.items()}
        if isinstance(value,Sequence) and not isinstance(value,(str,bytes,bytearray)):return [self.redact(item) for item in value]
        return value
    def contains_secret(self,value:Any)->bool:
        return self.redact(value)!=value

