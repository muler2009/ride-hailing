from django.urls import path

from apps.vehicles.views import (
    ActivateVehicleView,
    MyVehiclesView,
    VehicleDocumentUploadView,
    VehicleTypeListView,
)

urlpatterns = [
    path("vehicle-types/", VehicleTypeListView.as_view(), name="vehicle_type_list"),
    path("vehicles/me/", MyVehiclesView.as_view(), name="my_vehicles"),
    path("vehicles/<uuid:pk>/activate/", ActivateVehicleView.as_view(), name="vehicle_activate"),
    path(
        "vehicles/<uuid:pk>/documents/",
        VehicleDocumentUploadView.as_view(),
        name="vehicle_document_upload",
    ),
]
