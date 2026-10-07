from rest_framework import serializers

from apps.ratings.models import Rating


class SubmitRatingSerializer(serializers.Serializer):
    score = serializers.IntegerField(min_value=1, max_value=5)
    review = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class RatingSerializer(serializers.ModelSerializer):
    ride_id = serializers.UUIDField(source="ride.id", read_only=True)
    rater_email = serializers.SerializerMethodField()

    class Meta:
        model = Rating
        fields = ["id", "ride_id", "direction", "score", "review", "rater_email", "created_at"]
        read_only_fields = fields

    def get_rater_email(self, obj):
        return obj.rater.email if obj.rater_id else None
