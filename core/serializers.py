from rest_framework import serializers

ALLOWED_INTERVALS = (10, 25, 50)


class PlaceSerializer(serializers.Serializer):
    address = serializers.CharField()
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)


class TripRequestSerializer(serializers.Serializer):
    """
    Mirrors the frontend's TripRequest.

    Notes:
    - load_weight_lbs: FloatField also accepts numeric strings, since the
      form keeps weight as a string while the user types.
    - departure_time: a value without a timezone is treated as UTC (TIME_ZONE).
    - sample_interval_miles: optional; when left out, each route picks its own
      interval based on its length.
    """
    origin = PlaceSerializer()
    destination = PlaceSerializer()
    departure_time = serializers.DateTimeField()
    load_weight_lbs = serializers.FloatField()
    sample_interval_miles = serializers.ChoiceField(choices=ALLOWED_INTERVALS, required=False, allow_null=True)

    def validate_load_weight_lbs(self, value):
        if value <= 0:
            raise serializers.ValidationError("Must be greater than 0.")
        return value
