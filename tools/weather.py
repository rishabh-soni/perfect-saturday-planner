from agent.schemas import Schema


class WeatherArguments(Schema):
    """No model-supplied city/date override: always use the submitted window."""
    pass
