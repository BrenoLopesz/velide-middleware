#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
src/presenters/app_presenter.py

This module contains the main application presenter, AppPresenter.
It controls the overall application flow, switching between different
views (like authentication, dashboard, and setup) using a finite state machine.
"""

# ----------------------------------------------------------------------------
# Imports
# ----------------------------------------------------------------------------

# Third-party imports
from typing import Optional
from PyQt5.QtCore import QObject
from presenters.dashboard_presenter import DashboardPresenter
from presenters.device_code_presenter import DeviceCodePresenter
from services.auth_service import AuthService
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from states.main_state_machine import MainStateMachine
from visual.main_view import MainView

# ----------------------------------------------------------------------------
# Class Definition
# ----------------------------------------------------------------------------

class AppPresenter(QObject):
    """
    The main application presenter.

    This class orchestrates the high-level application logic. It uses a
    QStateMachine to manage the application's state, such as 'showing device code',
    'showing dashboard', or 'handling errors'. It coordinates multiple
    services and sub-presenters (like DeviceCodePresenter and DashboardPresenter)
    to build the complete application flow.
    """


    # ------------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------------

    def __init__(
            self,
            view: MainView,
            state_machine: MainStateMachine,
            auth_service: AuthService,
            deliverymen_retriever_service: DeliverymenRetrieverService,
            device_code_presenter: DeviceCodePresenter,
            dashboard_presenter: DashboardPresenter
    ):
        """
        Initializes the AppPresenter.

        Args:
            view: The main application window (MainView).
        """
        super().__init__()

        # --- Dependency Injection ---
        self._view = view
        self._auth_service = auth_service
        self._deliverymen_retriever_service = deliverymen_retriever_service
        self._dashboard_presenter = dashboard_presenter
        self._device_code_presenter = device_code_presenter

        self._last_error_title: Optional[str] = None
        self._last_error_message: Optional[str] = None

        # --- State Machine ---
        self._machine = state_machine

        # --- Setup ---
        self._connect_actions()

    def run(self):
        """
        Starts the application logic.

        This method starts the state machine and shows the main application window.
        """
        self._machine.start()
        self._view.show()

    def _connect_actions(self):
        """
        Connects state entry/exit signals and other signals to slots (methods).
        
        This defines *what happens* when a state is entered or a signal is emitted.
        """
        self._machine.device_flow_state.entered.connect(self._device_code_presenter.on_start)

        self._machine.check_mapping_state.entered.connect(
            self._deliverymen_retriever_service.check_if_mapping_is_required
        )
        self._machine.gathering_deliverymen_state.entered.connect(
            self._deliverymen_retriever_service.fetch_deliverymen
        )
        self._machine.dashboard_state.entered.connect(
            self._dashboard_presenter.start
        )

        # --- UI Screen Switching ---
        # When a state is entered, show the corresponding screen in the MainView.
        self._machine.initial_state.entered.connect(lambda: self._view.show_screen_by_index(0))
        self._machine.device_flow_state.entered.connect(lambda: self._view.show_screen_by_index(1))
        self._machine.dashboard_state.entered.connect(lambda: self._view.show_screen_by_index(3))
        self._machine.deliverymen_mapping_state.entered.connect(lambda: self._view.show_screen_by_index(4))

        self._auth_service.error.connect(lambda err: self._cache_error(err, "Não foi possível realizar<br/>a autenticação."))
        self._deliverymen_retriever_service.error.connect(
            lambda err: self._cache_error(err, "Não foi possível buscar<br/>os entregadores.")
        )

        # The error state's 'entered' signal has no arguments.
        # It triggers the method to show the screen using the cached message.
        self._machine.error_state.entered.connect(self._show_error_screen)

    def _cache_error(self, error_description: str, error_title: Optional[str]):
        self._last_error_message = error_description
        if error_title is not None:
            self._last_error_title = error_title

    def _show_error_screen(self):
        """
        Displays the error screen using the cached error message.
        This slot is connected to `error_state.entered` (which has no arguments).
        """
        if self._last_error_title is not None:
            self._view.device_code_error_screen.set_error_title(self._last_error_title)
            self._last_error_title = None
        if self._last_error_message is not None:
            self._view.device_code_error_screen.set_error_description(self._last_error_message)
            self._last_error_message = None
        self._view.show_screen_by_index(2)  # Index 2 is the error screen