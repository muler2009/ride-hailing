from rest_framework import generics, permissions, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.identity.permissions import HasPermission
from apps.pricing.models import Fare, PricingRule
from apps.pricing.serializers import FareSerializer, PricingRuleSerializer, PricingRuleUpsertSerializer
from apps.vehicles.models import VehicleType


class PricingRuleListView(generics.ListAPIView):
    """
    GET /api/v1/pricing-rules/ — public. Lets a client show "starting from
    $X" per vehicle type before a rider (or guest) even requests a ride,
    matching this platform's existing no-signup-required philosophy.
    """

    permission_classes = [permissions.AllowAny]
    serializer_class = PricingRuleSerializer
    pagination_class = None
    queryset = PricingRule.objects.select_related("vehicle_type")


class PricingRuleDetailView(APIView):
    """
    GET /api/v1/pricing-rules/{vehicle_type_id}/ — public.
    PUT /api/v1/pricing-rules/{vehicle_type_id}/ — requires `pricing.manage`;
    creates the rule if none exists yet for that vehicle type, otherwise
    replaces it wholesale (an upsert, not a partial patch).
    """

    def get_permissions(self):
        if self.request.method == "PUT":
            return [permissions.IsAuthenticated(), HasPermission()]
        return [permissions.AllowAny()]

    required_permission = "pricing.manage"

    def get(self, request, vehicle_type_id):
        try:
            rule = PricingRule.objects.select_related("vehicle_type").get(vehicle_type_id=vehicle_type_id)
        except PricingRule.DoesNotExist:
            raise NotFound("No pricing rule configured for this vehicle type.")
        return Response(PricingRuleSerializer(rule).data)

    def put(self, request, vehicle_type_id):
        try:
            vehicle_type = VehicleType.objects.get(id=vehicle_type_id)
        except VehicleType.DoesNotExist:
            raise NotFound("Unknown vehicle type.")

        serializer = PricingRuleUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        previous = PricingRule.objects.filter(vehicle_type=vehicle_type).first()
        before = (
            {
                "base_fare": str(previous.base_fare),
                "per_km_rate": str(previous.per_km_rate),
                "per_minute_rate": str(previous.per_minute_rate),
                "surge_multiplier": str(previous.surge_multiplier),
            }
            if previous
            else {}
        )

        rule, _created = PricingRule.objects.update_or_create(
            vehicle_type=vehicle_type, defaults=serializer.validated_data
        )

        from apps.admin_api.audit import record_audit

        record_audit(
            actor=request.user,
            action="PRICING_RULE_UPDATED",
            target_type="PricingRule",
            target_id=rule.id,
            changes={
                "vehicle_type": vehicle_type.name,
                "before": before,
                "after": {
                    "base_fare": str(rule.base_fare),
                    "per_km_rate": str(rule.per_km_rate),
                    "per_minute_rate": str(rule.per_minute_rate),
                    "surge_multiplier": str(rule.surge_multiplier),
                },
            },
        )

        return Response(PricingRuleSerializer(rule).data, status=status.HTTP_200_OK)


class RideFareListView(generics.ListAPIView):
    """
    GET /api/v1/rides/{id}/fares/ — every Fare recorded for a ride
    (typically an ESTIMATE, and later a FINAL and/or CANCELLATION). Same
    access rule as the ride detail view: the ride's own rider, its
    assigned driver, or `ride.manage`.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = FareSerializer
    pagination_class = None

    def get_queryset(self):
        from apps.identity.services import user_has_permission
        from apps.rides.models import Ride

        try:
            ride = Ride.objects.get(pk=self.kwargs["pk"])
        except Ride.DoesNotExist:
            raise NotFound("Ride not found.")

        driver = getattr(self.request.user, "driver_profile", None)
        is_own_rider = ride.rider_id == self.request.user.id
        is_assigned_driver = driver is not None and ride.driver_id == driver.id
        is_admin_viewer = user_has_permission(self.request.user, "ride.manage")

        if not (is_own_rider or is_assigned_driver or is_admin_viewer):
            raise PermissionDenied("You do not have access to this ride.")

        return Fare.objects.filter(ride=ride).prefetch_related("line_items")
