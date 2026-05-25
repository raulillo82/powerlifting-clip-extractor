import os

_GEOIP_PATH = os.environ.get("GEOIP_DB_PATH", "/app/GeoLite2-City.mmdb")
_reader = None
_reader_tried = False


def _get_reader():
    global _reader, _reader_tried
    if _reader_tried:
        return _reader
    _reader_tried = True
    if not os.path.exists(_GEOIP_PATH):
        return None
    try:
        import geoip2.database
        _reader = geoip2.database.Reader(_GEOIP_PATH)
    except Exception:
        pass
    return _reader


def lookup(ip: str) -> dict:
    """Return {city, country_code, lat, lng} or {} if GeoLite2 DB unavailable."""
    reader = _get_reader()
    if reader is None:
        return {}
    try:
        r = reader.city(ip)
        return {
            "city": r.city.name,
            "country_code": r.country.iso_code,
            "lat": r.location.latitude,
            "lng": r.location.longitude,
        }
    except Exception:
        return {}
