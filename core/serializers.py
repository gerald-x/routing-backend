from rest_framework import serializers


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
    """
    origin = PlaceSerializer()
    destination = PlaceSerializer()
    departure_time = serializers.DateTimeField()
    load_weight_lbs = serializers.FloatField()

    def validate_load_weight_lbs(self, value):
        if value <= 0:
            raise serializers.ValidationError("Must be greater than 0.")
        return value
