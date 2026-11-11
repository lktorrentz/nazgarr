import pytest

from nazgarr.library.guess import clean_name, guess
from nazgarr.upload.source import scan_source


@pytest.mark.parametrize(("name", "cleaned"), [
    ("The Matrix_t00.mkv", "The Matrix.mkv"),
    ("The Matrix (1999)_t01.mkv", "The Matrix (1999).mkv"),
    ("Inception t02.mkv", "Inception.mkv"),
    ("/data/rips/Show S01E01_t03.mkv", "/data/rips/Show S01E01.mkv"),
    ("Movie.2024_t100", "Movie.2024"),
    # Non è il suffisso di MakeMKV: resta.
    ("Matt00.mkv", "Matt00.mkv"),
    ("t00.mkv", "t00.mkv"),
    ("Show.S01E01.1080p.WEB-DL-GRP.mkv", "Show.S01E01.1080p.WEB-DL-GRP.mkv"),
])
def test_the_makemkv_title_suffix_goes(name, cleaned):
    assert clean_name(name) == cleaned


def test_the_title_is_read_without_it(tmp_path):
    assert guess("The Matrix_t00.mkv")["title"] == "The Matrix"
    assert "episode_title" not in guess("Show S01E01_t03.mkv")
    video = tmp_path / "Arrival (2016)_t00.mkv"
    video.write_bytes(b"x" * 1000)
    layout = scan_source(str(video))
    assert (layout.title, layout.year) == ("Arrival", 2016)


@pytest.mark.parametrize(("name", "season", "episode"), [
    # Il numero davanti è l'episodio, quelli del titolo no (segnalato
    # dall'utente, 2026-11-11: "426" diventava S04E26).
    ("08 - Sig. A in roco 426.mkv", None, 8),
    ("08 - Area 51.mkv", None, 8),
    ("08 - Volo 815.mkv", None, 8),
    ("08 - Agente 007.mkv", None, 8),
    # La stagione solo dalle cartelle.
    ("Serie/Season 01/08 - Sig. A in roco 426.mkv", 1, 8),
    ("Serie (2020)/Stagione 2/08 - Volo 815.mkv", 2, 8),
    ("Serie/08 - Volo 815.mkv", None, 8),
    # Com'erano: un nome senza numeri nel titolo, due episodi, stagione ed
    # episodio scritti nel nome.
    ("07 - Titolo.mkv", None, 7),
    ("08 - 09 - Titolo.mkv", None, [8, 9]),
    ("08-09 - Titolo.mkv", None, [8, 9]),
    ("01 - Serie S01E05.mkv", 1, 5),
    ("Serie S01E08 - Titolo 426.mkv", 1, 8),
    ("Serie 1x08 - Titolo, 426.mkv", 1, 8),
])
def test_a_leading_episode_number_wins_over_the_numbers_of_the_title(name, season, episode):
    result = guess(name)
    assert (result.get("season"), result.get("episode")) == (season, episode)


def test_a_season_folder_with_numbers_in_the_titles_is_one_season(tmp_path):
    folder = tmp_path / "Serie (2020)" / "Season 01"
    folder.mkdir(parents=True)
    names = [f"{n:02d} - Titolo {n}.mkv" for n in range(1, 15) if n != 8] + ["08 - Sig. A in roco 426.mkv"]
    for name in names:
        (folder / name).write_bytes(b"x" * 1000)
    layout = scan_source(str(folder))
    assert layout.episodes_by_season == {1: list(range(1, 15))}
    assert layout.kind == "season_pack"
