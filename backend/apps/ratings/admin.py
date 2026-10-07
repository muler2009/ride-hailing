from django.contrib import admin

from apps.ratings.models import Rating


@admin.register(Rating)
class RatingAdmin(admin.ModelAdmin):
    list_display = ["ride", "direction", "score", "rater", "ratee", "created_at"]
    list_filter = ["direction", "score"]
    search_fields = ["ride__id", "rater__email", "ratee__email"]
    autocomplete_fields = ["ride", "rater", "ratee"]
    readonly_fields = ["id", "created_at", "updated_at"]
