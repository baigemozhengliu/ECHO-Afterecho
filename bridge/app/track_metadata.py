"""Non-destructive corrections; callers compute identity from the raw record first."""
import re
from .core import unknown_artist


def effective_track(raw: dict, indexed: dict | None = None) -> dict:
    track = dict(raw)
    # Confirmed against the user's original pathname, not inferred from an album.
    numeric_credit = re.fullmatch(r'\(イチロクヨン\)&([^-]+)-(.+)', raw.get('title', ''))
    if numeric_credit and raw.get('trackNumber') == 164 and unknown_artist(raw.get('artists', [])):
        track.update(title=numeric_credit[2], artists=['164 (イチロクヨン)', numeric_credit[1]], trackNumber=None)
    # Nine filename records verified against the original inventory and Kuwo album.
    volcano_titles = {"Misty Memory(Acoustic Version)", "Drifting Blossom", "So Long for Another Summer",
                      "Effervescence", "Misty Memory(Day Version)", "Adele's Dream",
                      "Misty Memory(Night Version)", "Sheepnado Decimates Nomadic City", "Counting Sheep"}
    if raw.get('album') == '火山旅梦OST' and raw.get('artists') == ['塞壬唱片'] and raw.get('title', '').startswith('MSR&'):
        credits, separator, title = raw['title'].rpartition('-')
        if separator and title in volcano_titles:
            track.update(title=title, artists=['塞壬唱片-' + credits])
    details = indexed or {}
    if unknown_artist(track.get('artists', [])) and details.get('matchScore', 0) >= 85:
        artists = details.get('artists') or ([details['artist']] if details.get('artist') else [])
        if not unknown_artist(artists):
            track['artists'] = artists
    if not track.get('album') and details.get('album') and details.get('matchScore', 0) >= 85:
        track['album'] = details['album']
    if not track.get('durationMs') and details.get('durationMs'):
        track['durationMs'] = details['durationMs']
    return track
