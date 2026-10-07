from django.db.models import Avg, Count
from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ratings.models import Rating, RatingDirection
from apps.ratings.serializers import RatingSerializer, SubmitRatingSerializer
from apps.ratings.services import RatingError, submit_rating
from apps.rides.models import Ride


def _get_ride_or_404(pk):
    try:
        return Ride.objects.get(pk=pk)
    except Ride.DoesNotExist:
        raise NotFound("Ride not found.")


def _get_ride_by_token_or_404(tracking_token):
    try:
        return Ride.objects.get(tracking_token=tracking_token)
    except (Ride.DoesNotExist, ValueError, ValidationError):
        raise NotFound("No ride found for this tracking token.")


def _rating_error_response(exc: RatingError) -> Response:
    return Response(
        {"error": {"code": "rating_error", "message": str(exc), "details": None}},
        status=status.HTTP_400_BAD_REQUEST,
    )


class RideRatingsView(APIView):
    """
    GET  /api/v1/rides/{id}/ratings/  — both directions' ratings for a ride.
    POST /api/v1/rides/{id}/ratings/  — submit a rating. The direction is
    derived from who the caller is (the ride's rider vs. its assigned
    driver), never taken from the request body — a caller can't choose to
    submit "as" the other party.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        ride = _get_ride_or_404(pk)
        self._require_access(request, ride)
        return Response(RatingSerializer(ride.ratings.all(), many=True).data)

    def post(self, request, pk):
        ride = _get_ride_or_404(pk)
        direction = self._resolve_direction(request, ride)

        serializer = SubmitRatingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            rating = submit_rating(
                ride,
                direction=direction,
                score=serializer.validated_data["score"],
                review=serializer.validated_data.get("review", ""),
                rater=request.user,
            )
        except RatingError as exc:
            return _rating_error_response(exc)

        return Response(RatingSerializer(rating).data, status=status.HTTP_201_CREATED)

    def _resolve_direction(self, request, ride) -> str:
        driver = getattr(request.user, "driver_profile", None)
        if ride.rider_id == request.user.id:
            return RatingDirection.RIDER_TO_DRIVER
        if driver is not None and ride.driver_id == driver.id:
            return RatingDirection.DRIVER_TO_RIDER
        raise PermissionDenied("You are not a party to this ride.")

    def _require_access(self, request, ride):
        from apps.identity.services import user_has_permission

        driver = getattr(request.user, "driver_profile", None)
        is_own_rider = ride.rider_id == request.user.id
        is_assigned_driver = driver is not None and ride.driver_id == driver.id
        is_admin_viewer = user_has_permission(request.user, "ride.manage")
        if not (is_own_rider or is_assigned_driver or is_admin_viewer):
            raise PermissionDenied("You do not have access to this ride.")


class GuestRideRatingView(APIView):
    """
    POST /api/v1/rides/track/{tracking_token}/ratings/ — a guest rider
    rating their driver, authorized by the tracking token rather than a
    login session, consistent with every other guest-facing action since
    Phase 3. Guests can only ever submit RIDER_TO_DRIVER.
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request, tracking_token):
        ride = _get_ride_by_token_or_404(tracking_token)

        serializer = SubmitRatingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            rating = submit_rating(
                ride,
                direction=RatingDirection.RIDER_TO_DRIVER,
                score=serializer.validated_data["score"],
                review=serializer.validated_data.get("review", ""),
                guest_phone_number=ride.guest_phone_number,
            )
        except RatingError as exc:
            return _rating_error_response(exc)

        return Response(RatingSerializer(rating).data, status=status.HTTP_201_CREATED)


class DriverRatingSummaryView(APIView):
    """
    GET /api/v1/drivers/{id}/ratings/ — public. A driver's aggregate
    rating and recent reviews, so a rider can see who they're being
    matched with. Reviewer identities aren't exposed — only scores and
    review text, since a rider shouldn't be able to work out which
    specific passenger left which comment.
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        from apps.drivers.models import Driver

        try:
            driver = Driver.objects.select_related("user").get(pk=pk)
        except Driver.DoesNotExist:
            raise NotFound("Driver not found.")

        ratings = Rating.objects.filter(ratee=driver.user, direction=RatingDirection.RIDER_TO_DRIVER)
        aggregate = ratings.aggregate(average=Avg("score"), count=Count("id"))
        recent_reviews = list(
            ratings.exclude(review="").order_by("-created_at").values("score", "review", "created_at")[:10]
        )

        return Response(
            {
                "driver_id": str(driver.id),
                "average_rating": round(aggregate["average"], 2) if aggregate["average"] is not None else None,
                "rating_count": aggregate["count"],
                "recent_reviews": recent_reviews,
            }
        )


class MyRatingsView(APIView):
    """GET /api/v1/ratings/me/ — ratings the caller has received, with their average."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        received = Rating.objects.filter(ratee=request.user)
        aggregate = received.aggregate(average=Avg("score"), count=Count("id"))
        return Response(
            {
                "average_rating": round(aggregate["average"], 2) if aggregate["average"] is not None else None,
                "rating_count": aggregate["count"],
                "ratings": RatingSerializer(received, many=True).data,
            }
        )
