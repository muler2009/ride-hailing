from rest_framework import serializers


class GeocodeRequestSerializer(serializers.Serializer):
    address = serializers.CharField(max_length=500)


class GeocodeResponseSerializer(serializers.Serializer):
    formatted_address = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()


class ReverseGeocodeRequestSerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)


class ReverseGeocodeResponseSerializer(serializers.Serializer):
    formatted_address = serializers.CharField()


class RouteRequestSerializer(serializers.Serializer):
    origin_latitude = serializers.FloatField(min_value=-90, max_value=90)
    origin_longitude = serializers.FloatField(min_value=-180, max_value=180)
    destination_latitude = serializers.FloatField(min_value=-90, max_value=90)
    destination_longitude = serializers.FloatField(min_value=-180, max_value=180)


class RouteResponseSerializer(serializers.Serializer):
    distance_km = serializers.FloatField()
    duration_minutes = serializers.FloatField()
    polyline = serializers.CharField(allow_null=True)
