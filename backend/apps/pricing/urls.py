from django.urls import path

from apps.pricing.views import PricingRuleDetailView, PricingRuleListView, RideFareListView

urlpatterns = [
    path("pricing-rules/", PricingRuleListView.as_view(), name="pricing_rule_list"),
    path("pricing-rules/<uuid:vehicle_type_id>/", PricingRuleDetailView.as_view(), name="pricing_rule_detail"),
    path("rides/<uuid:pk>/fares/", RideFareListView.as_view(), name="ride_fare_list"),
]
