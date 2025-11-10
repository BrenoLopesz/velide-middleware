from typing import Optional
from pydantic import BaseModel, ConfigDict

from presenters.app_presenter import AppPresenter
from presenters.dashboard_presenter import DashboardPresenter
from presenters.deliverymen_mapping_presenter import DeliverymenMappingPresenter
from presenters.device_code_presenter import DeviceCodePresenter

from services.auth_service import AuthService
from services.deliveries_service import DeliveriesService
from services.deliverymen_retriever_service import DeliverymenRetrieverService
from services.sqlite_service import SQLiteService
from services.tracking_persistence_service import TrackingPersistenceService


class Presenters(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    app: Optional[AppPresenter] = None
    dashboard: DashboardPresenter
    deliverymen_mapping: DeliverymenMappingPresenter
    device_code: DeviceCodePresenter

class Services(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    auth: AuthService
    deliveries: DeliveriesService
    deliverymen_retriever: DeliverymenRetrieverService
    sqlite: SQLiteService
    # tracking_persistance: TrackingPersistenceService