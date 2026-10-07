from django.urls import path

from apps.maps.views import GeocodeView, ReverseGeocodeView, RouteView

urlpatterns = [
    path("maps/geocode/", GeocodeView.as_view(), name="maps_geocode"),
    path("maps/reverse-geocode/", ReverseGeocodeView.as_view(), name="maps_reverse_geocode"),
    path("maps/route/", RouteView.as_view(), name="maps_route"),
]
