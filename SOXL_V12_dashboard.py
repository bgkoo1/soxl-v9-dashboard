"""Compatibility entrypoint for the standalone V12 rebuild."""
from v12.app import render as render_v12_dashboard

if __name__ == "__main__":
    render_v12_dashboard()
