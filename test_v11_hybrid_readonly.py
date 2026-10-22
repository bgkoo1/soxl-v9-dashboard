import ast
from pathlib import Path
def test_app_syntax():
    ast.parse(Path("SOXL_V11_hybrid_readonly.py").read_text(encoding="utf-8"))
def test_no_order_calls():
    s=Path("SOXL_V11_hybrid_readonly.py").read_text(encoding="utf-8")
    assert "requests.post(" not in s
    assert "requests.put(" not in s
    assert "requests.patch(" not in s
    assert "requests.delete(" not in s
    assert "live_portfolio.enc" in s
