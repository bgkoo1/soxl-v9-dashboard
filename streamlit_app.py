"""Deployment entrypoint for V12-LIVE-DESK-20261028.

Reload a cached older workspace once after deployment; never silently render an
older dashboard when this entrypoint requires the live account workspace.
"""
import importlib
import streamlit as st
import v12.app as dashboard

EXPECTED_BUILD = "V12-LIVE-DESK-20261028"
if getattr(dashboard, "BUILD", None) != EXPECTED_BUILD:
    for name in ("v12.engine", "v12.meritz", "v12.broker_book", "v12.desk_model",
                 "v12.broker_ui", "v12.live_desk", "v12.app"):
        importlib.reload(importlib.import_module(name))
    dashboard = importlib.import_module("v12.app")

if getattr(dashboard, "BUILD", None) != EXPECTED_BUILD:
    st.error("배포 파일이 최신 V12 운용 화면과 일치하지 않습니다. Streamlit 앱을 Reboot해 주세요.")
    st.stop()

dashboard.render()
