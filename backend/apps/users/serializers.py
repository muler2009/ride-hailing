from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.identity.services import get_user_permission_codenames
from apps.users.models import User
from apps.users.services import register_user


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    phone_number = serializers.CharField(max_length=32, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, min_length=8)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    role = serializers.ChoiceField(
        choices=["Rider", "Driver"],
        default="Rider",
        help_text="Which base role to assign at registration. Fleet/staff roles are assigned by admins, not at signup.",
    )

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone_number(self, value):
        if value and User.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        return register_user(**validated_data)


class UserSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "phone_number",
            "first_name",
            "last_name",
            "is_active",
            "is_suspended",
            "roles",
            "permissions",
            "created_at",
        ]
        read_only_fields = fields

    def get_permissions(self, obj):
        return sorted(get_user_permission_codenames(obj))

    def get_roles(self, obj):
        return sorted(obj.user_roles.values_list("role__name", flat=True))


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()
