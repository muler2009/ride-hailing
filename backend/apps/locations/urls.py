from django.urls import path

from apps.locations.views import DriverLocationUpdateView, RideLocationHistoryView

urlpatterns = [
    path("drivers/me/location/", DriverLocationUpdateView.as_view(), name="driver_location_update"),
    path("rides/<uuid:pk>/locations/", RideLocationHistoryView.as_view(), name="ride_location_history"),
]
