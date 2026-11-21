"""The local data-testing engine: plan checks from a contract, evaluate them on a backend.

Pure by design (D-18, D-32): no click, rich or network code here, and no pandas or polars; a
dataset reaches the engine as an Arrow table.
"""
