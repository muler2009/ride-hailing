from django.urls import path

from apps.drivers.views import (
    AdminDriverListView,
    ApproveDriverView,
    DriverAvailabilityView,
    DriverDocumentUploadView,
    MyDriverProfileView,
    ReactivateDriverView,
    RejectDriverView,
    SuspendDriverView,
)

urlpatterns = [
    path("drivers/me/", MyDriverProfileView.as_view(), name="my_driver_profile"),
    path("drivers/me/documents/", DriverDocumentUploadView.as_view(), name="driver_document_upload"),
    path("drivers/me/availability/", DriverAvailabilityView.as_view(), name="driver_availability"),
    # Administrative approval workflow
    path("admin/drivers/", AdminDriverListView.as_view(), name="admin_driver_list"),
    path("admin/drivers/<uuid:pk>/approve/", ApproveDriverView.as_view(), name="admin_driver_approve"),
    path("admin/drivers/<uuid:pk>/reject/", RejectDriverView.as_view(), name="admin_driver_reject"),
    path("admin/drivers/<uuid:pk>/suspend/", SuspendDriverView.as_view(), name="admin_driver_suspend"),
    path(
        "admin/drivers/<uuid:pk>/reactivate/",
        ReactivateDriverView.as_view(),
        name="admin_driver_reactivate",
    ),
]
