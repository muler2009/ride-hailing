from django.urls import path

from apps.rides.views import (
    AdminRideListView,
    AssignDriverView,
    ExpireRideView,
    GuestRideCancelView,
    GuestRideTrackView,
    MarkNoDriverFoundView,
    MyRidesListView,
    RideAcceptView,
    RideArrivedView,
    RideArrivingView,
    RideCancelView,
    RideCompleteView,
    RideCreateView,
    RideDeclineView,
    RideDetailView,
    RideStartView,
)

urlpatterns = [
    # Creation & registered-rider access
    path("rides/", RideCreateView.as_view(), name="ride_create"),
    path("rides/me/", MyRidesListView.as_view(), name="my_rides"),
    path("rides/<uuid:pk>/", RideDetailView.as_view(), name="ride_detail"),
    path("rides/<uuid:pk>/cancel/", RideCancelView.as_view(), name="ride_cancel"),
    # Guest self-service (no login — authenticated by tracking_token)
    path("rides/track/<uuid:tracking_token>/", GuestRideTrackView.as_view(), name="guest_ride_track"),
    path(
        "rides/track/<uuid:tracking_token>/cancel/",
        GuestRideCancelView.as_view(),
        name="guest_ride_cancel",
    ),
    # Driver lifecycle actions
    path("rides/<uuid:pk>/accept/", RideAcceptView.as_view(), name="ride_accept"),
    path("rides/<uuid:pk>/decline/", RideDeclineView.as_view(), name="ride_decline"),
    path("rides/<uuid:pk>/arriving/", RideArrivingView.as_view(), name="ride_arriving"),
    path("rides/<uuid:pk>/arrived/", RideArrivedView.as_view(), name="ride_arrived"),
    path("rides/<uuid:pk>/start/", RideStartView.as_view(), name="ride_start"),
    path("rides/<uuid:pk>/complete/", RideCompleteView.as_view(), name="ride_complete"),
    # Admin / operations — Phase 3 manual dispatch stand-in
    path("admin/rides/", AdminRideListView.as_view(), name="admin_ride_list"),
    path("admin/rides/<uuid:pk>/assign-driver/", AssignDriverView.as_view(), name="admin_ride_assign_driver"),
    path(
        "admin/rides/<uuid:pk>/mark-no-driver-found/",
        MarkNoDriverFoundView.as_view(),
        name="admin_ride_mark_no_driver_found",
    ),
    path("admin/rides/<uuid:pk>/expire/", ExpireRideView.as_view(), name="admin_ride_expire"),
]
