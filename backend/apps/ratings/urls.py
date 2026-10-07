from django.urls import path

from apps.ratings.views import DriverRatingSummaryView, GuestRideRatingView, MyRatingsView, RideRatingsView

urlpatterns = [
    path("rides/<uuid:pk>/ratings/", RideRatingsView.as_view(), name="ride_ratings"),
    path("rides/track/<uuid:tracking_token>/ratings/", GuestRideRatingView.as_view(), name="guest_ride_rating"),
    path("drivers/<uuid:pk>/ratings/", DriverRatingSummaryView.as_view(), name="driver_rating_summary"),
    path("ratings/me/", MyRatingsView.as_view(), name="my_ratings"),
]
