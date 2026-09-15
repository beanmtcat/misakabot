from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import date, datetime, timezone

import psycopg
from pypinyin import Style, lazy_pinyin
from psycopg.rows import dict_row

from .emby_client import LoginSession, WatchSession


_SHANGHAI_CURRENT_DATE = "(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date"
_MISSING_AIRED_CONDITION = "s.official_latest > 0 AND COALESCE(s.server_latest,0) < s.official_latest"
_OVERDUE_CONDITION = f"s.next_update IS NOT NULL AND s.next_update < {_SHANGHAI_CURRENT_DATE}"
# A season with a known total but neither a remaining schedule nor all episodes
# aired is not silently treated as completed.  TMDB has incomplete metadata for
# some titles, and this makes those rows visible for review.
_STALE_METADATA_CONDITION = (
    "s.next_update IS NULL AND COALESCE(s.total,0) > 0 "
    "AND COALESCE(s.official_latest,0) < s.total"
)
_EXCEPTION_CONDITION = (
    f"({_OVERDUE_CONDITION}) OR ({_MISSING_AIRED_CONDITION}) OR ({_STALE_METADATA_CONDITION})"
)


class LegacyEmbyRepository:
    """Uses only the existing Dragonli Emby tables; this module intentionally contains no DDL."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def upsert_user(self, payload: Mapping[str, object]) -> bool:
        user_id, server_id, username = (_string(payload.get(key)) for key in ("Id", "ServerId", "Name"))
        if not all((user_id, server_id, username)):
            return False
        policy = payload.get("Policy")
        values = policy if isinstance(policy, Mapping) else {}
        self._write(
            """
            INSERT INTO dragonli_emby_users (
              id,username,server_id,prefix,date_created,last_login,last_activity,has_password,
              has_configured_password,has_easy_password,is_admin,is_hidden,is_disabled,
              enable_remote_access,enable_media_playback,enable_video_transcoding,
              enable_audio_transcoding,mtime,isvalid
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),1)
            ON CONFLICT (id) DO UPDATE SET
              username=EXCLUDED.username,server_id=EXCLUDED.server_id,prefix=EXCLUDED.prefix,
              date_created=EXCLUDED.date_created,last_login=EXCLUDED.last_login,
              last_activity=EXCLUDED.last_activity,has_password=EXCLUDED.has_password,
              has_configured_password=EXCLUDED.has_configured_password,
              has_easy_password=EXCLUDED.has_easy_password,is_admin=EXCLUDED.is_admin,
              is_hidden=EXCLUDED.is_hidden,is_disabled=EXCLUDED.is_disabled,
              enable_remote_access=EXCLUDED.enable_remote_access,
              enable_media_playback=EXCLUDED.enable_media_playback,
              enable_video_transcoding=EXCLUDED.enable_video_transcoding,
              enable_audio_transcoding=EXCLUDED.enable_audio_transcoding,mtime=NOW(),isvalid=1
            """,
            (user_id, username, server_id, _nullable(payload.get("Prefix")), _nullable(payload.get("DateCreated")),
             _nullable(payload.get("LastLoginDate")), _nullable(payload.get("LastActivityDate")),
             _bool(payload.get("HasPassword")), _bool(payload.get("HasConfiguredPassword")),
             _bool(payload.get("HasConfiguredEasyPassword")), _bool(values.get("IsAdministrator")),
             _bool(values.get("IsHidden")), _bool(values.get("IsDisabled")),
             _bool(values.get("EnableRemoteAccess")), _bool(values.get("EnableMediaPlayback")),
             _bool(values.get("EnableVideoPlaybackTranscoding")), _bool(values.get("EnableAudioPlaybackTranscoding"))),
        )
        return True

    def login_password_hash(self, username: str) -> str | None:
        row = self._one(
            "SELECT password FROM dragonli_users WHERE username=%s AND islogin=TRUE", (username,)
        )
        value = row["password"] if row else None
        return value if isinstance(value, str) else None

    def is_login_enabled(self, username: str) -> bool:
        return self._one(
            "SELECT 1 FROM dragonli_users WHERE username=%s AND islogin=TRUE", (username,)
        ) is not None

    def upsert_network_stats(
        self, interface_name: str, stats: list[tuple[date, int, int, float]]
    ) -> int:
        """Persist vnStat daily counters using the existing interface/date unique index."""
        with closing(psycopg.connect(self._database_url)) as connection:
            for stat_date, rx_bytes, tx_bytes, avg_rate in stats:
                total_bytes = rx_bytes + tx_bytes
                connection.execute(
                    """INSERT INTO dragonli_network_stats
                    (interface_name,stat_date,rx_bytes,tx_bytes,total_bytes,avg_rate,rx_gb,tx_gb,total_gb,rtime)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (interface_name,stat_date) DO UPDATE SET
                      rx_bytes=EXCLUDED.rx_bytes,tx_bytes=EXCLUDED.tx_bytes,
                      total_bytes=EXCLUDED.total_bytes,avg_rate=EXCLUDED.avg_rate,
                      rx_gb=EXCLUDED.rx_gb,tx_gb=EXCLUDED.tx_gb,total_gb=EXCLUDED.total_gb,rtime=NOW()""",
                    (
                        interface_name,
                        stat_date,
                        rx_bytes,
                        tx_bytes,
                        total_bytes,
                        avg_rate,
                        round(rx_bytes / 1024**3, 2),
                        round(tx_bytes / 1024**3, 2),
                        round(total_bytes / 1024**3, 2),
                    ),
                )
            connection.commit()
        return len(stats)

    def save_watch_session(self, session: WatchSession) -> bool:
        existing = self._one(
            "SELECT id FROM dragonli_emby_watch_logs WHERE session_id=%s AND item_id=%s ORDER BY id DESC LIMIT 1",
            (session.session_id, session.item_id),
        )
        if existing is None:
            self._write(
                """INSERT INTO dragonli_emby_watch_logs
                (user_id,username,item_id,item_name,item_type,play_start_time,play_end_time,play_duration,
                 play_progress,play_position,total_duration,ip_address,device_name,client_name,session_id,is_completed)
                VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,FALSE)""",
                _session_values(session),
            )
            return True
        self._write(
            """UPDATE dragonli_emby_watch_logs SET play_start_time=LEAST(play_start_time,%s),
               play_duration=%s,play_progress=%s,play_position=%s,
               total_duration=%s,ip_address=%s,device_name=%s,client_name=%s,play_end_time=NULL,is_completed=FALSE
               WHERE id=%s""",
            (session.play_start_time, *_session_values(session)[6:13], existing["id"]),
        )
        return False

    def finish_absent_sessions(self, active: set[tuple[str, str]]) -> int:
        rows = self._all(
            "SELECT id,session_id,item_id,play_progress FROM dragonli_emby_watch_logs "
            "WHERE play_end_time IS NULL AND session_id IS NOT NULL AND session_id != ''"
        )
        ended = 0
        for row in rows:
            if (str(row["session_id"]), str(row["item_id"])) in active:
                continue
            progress = float(row["play_progress"] or 0)
            self._write(
                """UPDATE dragonli_emby_watch_logs SET play_end_time=NOW(),
                   play_duration=COALESCE(play_position,play_duration,0),is_completed=%s
                   WHERE id=%s AND play_end_time IS NULL""",
                (progress >= 0.9, row["id"]),
            )
            ended += 1
        return ended

    def list_users(self, page: int, size: int, query: str | None, disabled: bool | None) -> dict[str, object]:
        clauses, parameters = ["isvalid=1"], []
        if query:
            clauses.append("username ILIKE %s"); parameters.append(f"%{query}%")
        if disabled is not None:
            clauses.append("is_disabled=%s"); parameters.append(disabled)
        where = " AND ".join(clauses)
        total = self._one(f"SELECT COUNT(*) AS count FROM dragonli_emby_users WHERE {where}", parameters)
        rows = self._all(
            f"""SELECT id::text AS id,replace(id::text,'-','') AS compact_id,username,server_id::text AS server_id,
            date_created,last_login,last_activity,is_admin,is_hidden,is_disabled,enable_remote_access,
            enable_media_playback,enable_video_transcoding,enable_audio_transcoding,mtime
            FROM dragonli_emby_users WHERE {where}
            ORDER BY last_activity DESC NULLS LAST,username ASC LIMIT %s OFFSET %s""",
            [*parameters, size, (page - 1) * size],
        )
        return {"items": rows, "total": int(total["count"]) if total else 0, "page": page, "page_size": size}

    def list_watch_logs(self, page: int, size: int, query: str | None, item_type: str | None,
                        start_at: str | None, end_at: str | None, item_id: str | None = None) -> dict[str, object]:
        clauses, parameters = ["1=1"], []
        if query:
            clauses.append("(username ILIKE %s OR item_name ILIKE %s)"); parameters.extend((f"%{query}%", f"%{query}%"))
        if item_type:
            clauses.append("item_type=%s"); parameters.append(item_type)
        if item_id:
            clauses.append("item_id=%s"); parameters.append(item_id)
        if start_at:
            clauses.append("play_start_time >= %s"); parameters.append(start_at)
        if end_at:
            clauses.append("play_start_time <= %s"); parameters.append(end_at)
        where = " AND ".join(clauses)
        total = self._one(f"SELECT COUNT(*) AS count FROM dragonli_emby_watch_logs WHERE {where}", parameters)
        rows = self._all(
            f"""SELECT id,user_id,username,item_id,item_name,item_type,play_start_time,play_end_time,
            play_duration,play_progress,play_position,total_duration,ip_address::text AS ip_address,
            device_name,client_name,session_id,is_completed,ctime FROM dragonli_emby_watch_logs WHERE {where}
            ORDER BY play_start_time DESC,id DESC LIMIT %s OFFSET %s""",
            [*parameters, size, (page - 1) * size],
        )
        return {"items": rows, "total": int(total["count"]) if total else 0, "page": page, "page_size": size}

    def save_login_session(self, session: LoginSession) -> bool:
        existing = self._one(
            "SELECT id FROM dragonli_emby_login_logs WHERE session_id=%s LIMIT 1", (session.session_id,)
        )
        if existing is not None:
            return False
        self._write(
            '''INSERT INTO dragonli_emby_login_logs
            (user_id,username,login_time,ip_address,device_name,client_name,session_id,is_success)
            VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE)''',
            (session.user_id, session.username, session.login_time, session.ip_address,
             session.device_name, session.client_name, session.session_id),
        )
        return True

    def list_login_logs(self, page: int, size: int, query: str | None) -> dict[str, object]:
        clauses, parameters = ["1=1"], []
        if query:
            clauses.append("(username ILIKE %s OR device_name ILIKE %s OR ip_address::text ILIKE %s)")
            parameters.extend((f"%{query}%", f"%{query}%", f"%{query}%"))
        where = " AND ".join(clauses)
        total = self._one(f"SELECT COUNT(*) AS count FROM dragonli_emby_login_logs WHERE {where}", parameters)
        rows = self._all(
            f'''SELECT id,user_id,username,login_time,logout_time,ip_address::text AS ip_address,
            device_name,client_name,session_id,is_success,error_message,ctime
            FROM dragonli_emby_login_logs WHERE {where}
            ORDER BY login_time DESC,id DESC LIMIT %s OFFSET %s''',
            [*parameters, size, (page - 1) * size],
        )
        return {"items": rows, "total": int(total["count"]) if total else 0, "page": page, "page_size": size}

    def dashboard(self) -> dict[str, object]:
        counts = self._one(
            """SELECT
                (SELECT COUNT(*) FROM dragonli_emby_users WHERE isvalid=1) AS users,
                (SELECT COUNT(*) FROM dragonli_emby_movies WHERE isvalid=1) AS movies,
                (SELECT COUNT(*) FROM dragonli_emby_series WHERE isvalid=1) AS series,
                (SELECT COUNT(*) FROM dragonli_emby_series WHERE isvalid=1 AND "update" IS TRUE) AS tracking,
                (SELECT COUNT(*) FROM dragonli_emby_episodes WHERE isvalid=1) AS episodes,
                (SELECT COUNT(*) FROM dragonli_emby_watch_logs WHERE play_end_time IS NULL) AS watching"""
        ) or {}
        recent_movies = self._all(
            """SELECT id::text AS id,name,date_created,size,container
            FROM dragonli_emby_movies WHERE isvalid=1
            ORDER BY date_created DESC NULLS LAST LIMIT 5"""
        )
        recent_episodes = self._all(
            """SELECT e.id,e.name AS episode_name,e.parent_index_number AS season_number,
            e.index_number AS episode_number,e.date_created,
            COALESCE(NULLIF(s.name,''),e.series_name) AS series_name,s.alias AS scraped_year
            FROM dragonli_emby_episodes e
            LEFT JOIN dragonli_emby_series s ON s.id=e.series_id
            WHERE e.isvalid=1 AND COALESCE(s.isvalid,1)=1
            ORDER BY e.date_created DESC NULLS LAST LIMIT 5"""
        )
        traffic_today = self._one(
            """SELECT COALESCE(SUM(rx_bytes),0) AS rx_bytes,COALESCE(SUM(tx_bytes),0) AS tx_bytes,
            COALESCE(SUM(total_bytes),0) AS total_bytes,COALESCE(AVG(avg_rate),0) AS avg_rate,
            MAX(rtime) AS updated_at FROM dragonli_network_stats WHERE stat_date=CURRENT_DATE"""
        ) or {}
        traffic_month = self._one(
            """SELECT COALESCE(SUM(rx_bytes),0) AS rx_bytes,COALESCE(SUM(tx_bytes),0) AS tx_bytes,
            COALESCE(SUM(total_bytes),0) AS total_bytes FROM dragonli_network_stats
            WHERE stat_date >= DATE_TRUNC('month', CURRENT_DATE)::date
              AND stat_date < (DATE_TRUNC('month', CURRENT_DATE) + INTERVAL '1 month')::date"""
        ) or {}
        traffic_recent = self._all(
            """SELECT stat_date::text AS date,COALESCE(SUM(rx_bytes),0) AS rx_bytes,
            COALESCE(SUM(tx_bytes),0) AS tx_bytes,COALESCE(SUM(total_bytes),0) AS total_bytes
            FROM dragonli_network_stats WHERE stat_date >= CURRENT_DATE - 6
            GROUP BY stat_date ORDER BY stat_date"""
        )
        return {
            "counts": {key: int(counts.get(key) or 0) for key in ("users", "movies", "series", "tracking", "episodes", "watching")},
            "recent_movies": recent_movies,
            "recent_episodes": recent_episodes,
            "traffic": {
                "today": _traffic_values(traffic_today, include_rate=True),
                "month": _traffic_values(traffic_month),
                "recent": [_traffic_values(row, include_date=True) for row in traffic_recent],
            },
        }

    def list_movies(self, page: int, size: int, query: str | None) -> dict[str, object]:
        clauses, parameters = ["m.isvalid=1"], []
        if query:
            clauses.append("m.name ILIKE %s")
            parameters.append(f"%{query}%")
        where = " AND ".join(clauses)
        total = self._one(f"SELECT COUNT(*) AS count FROM dragonli_emby_movies m WHERE {where}", parameters)
        rows = self._all(
            f"""SELECT m.id::text AS id,m.name,m.server_id,m.date_created,m.container,m.size,m.runtime_ticks,m.media_type,
            COALESCE(w.view_count,0) AS view_count,w.last_played
            FROM dragonli_emby_movies m
            LEFT JOIN (
              SELECT item_id,COUNT(*) AS view_count,MAX(play_start_time) AS last_played
              FROM dragonli_emby_watch_logs WHERE item_type='Movie' GROUP BY item_id
            ) w ON w.item_id=m.id::text
            WHERE {where}
            ORDER BY m.date_created DESC NULLS LAST,m.name ASC LIMIT %s OFFSET %s""",
            [*parameters, size, (page - 1) * size],
        )
        return {"items": rows, "total": int(total["count"]) if total else 0, "page": page, "page_size": size}

    def library_subfolder_ids(self) -> set[int]:
        rows = self._all("SELECT seq FROM dragonli_library_subfolders")
        return {int(row["seq"]) for row in rows if _positive_int(row.get("seq")) is not None}

    def upsert_emby_movies(self, payloads: list[Mapping[str, object]]) -> dict[str, int]:
        """Upsert Emby movies and their sources using the existing legacy tables.

        ``date_created`` deliberately means local import/update time here because
        that is how the original ``syncMovie`` cron task exposed newly changed files.
        """
        movies: list[tuple[int, tuple[object, ...], list[tuple[object, ...]]]] = []
        skipped = source_skipped = 0
        for payload in payloads:
            movie_id = _positive_int(payload.get("Id"))
            name = _string(payload.get("Name"))[:200]
            if movie_id is None or not name:
                skipped += 1
                continue
            providers = payload.get("ProviderIds")
            provider_ids = providers if isinstance(providers, Mapping) else {}
            tmdb_id = next(
                (_string(provider_ids.get(key)) for key in ("Tmdb", "TMDB", "tmdb") if _string(provider_ids.get(key))),
                None,
            )
            images = payload.get("ImageTags")
            image_tags = images if isinstance(images, Mapping) else {}
            movie_values = (
                name,
                _nullable(payload.get("ServerId")),
                _nullable(payload.get("Container")),
                _non_negative_int(payload.get("RunTimeTicks")),
                _non_negative_int(payload.get("Size")),
                _non_negative_int(payload.get("Bitrate")),
                _bool(payload.get("IsFolder")) if isinstance(payload.get("IsFolder"), bool) else False,
                _nullable(payload.get("MediaType")),
                _nullable(image_tags.get("Primary")),
                _nullable(image_tags.get("Logo")),
                _nullable(image_tags.get("Thumb")),
                tmdb_id,
                _positive_int(payload.get("ParentId")),
            )
            source_values: list[tuple[object, ...]] = []
            media_sources = payload.get("MediaSources")
            if isinstance(media_sources, list):
                for source in media_sources:
                    if not isinstance(source, Mapping):
                        source_skipped += 1
                        continue
                    source_id = _string(source.get("Id"))
                    if not source_id:
                        source_skipped += 1
                        continue
                    source_values.append((
                        source_id, str(movie_id), _nullable(source.get("Path")), _nullable(source.get("Container")),
                        _non_negative_int(source.get("Size")), _nullable(source.get("Name")),
                        _bool(source.get("IsRemote")), _bool(source.get("SupportsTranscoding")),
                        _bool(source.get("SupportsDirectStream")), _bool(source.get("SupportsDirectPlay")),
                    ))
            movies.append((movie_id, movie_values, source_values))
        if not movies:
            return {"created": 0, "updated": 0, "skipped": skipped, "sources": 0, "source_skipped": source_skipped}

        movie_ids = [movie_id for movie_id, _, _ in movies]
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            with connection.transaction():
                current_rows = connection.execute(
                    "SELECT id FROM dragonli_emby_movies WHERE id = ANY(%s)", (movie_ids,)
                ).fetchall()
                existing_ids = {int(row["id"]) for row in current_rows}
                with connection.cursor() as cursor:
                    cursor.executemany(
                        '''INSERT INTO dragonli_emby_movies
                        (id,name,server_id,container,runtime_ticks,size,bitrate,is_folder,media_type,
                         image_primary,image_logo,image_thumb,tmdb_id,parent_id,ctime,mtime,isvalid,date_created)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),1,NOW())
                        ON CONFLICT (id) DO UPDATE SET
                          name=EXCLUDED.name,server_id=EXCLUDED.server_id,container=EXCLUDED.container,
                          runtime_ticks=EXCLUDED.runtime_ticks,size=EXCLUDED.size,bitrate=EXCLUDED.bitrate,
                          is_folder=EXCLUDED.is_folder,media_type=EXCLUDED.media_type,
                          image_primary=EXCLUDED.image_primary,image_logo=EXCLUDED.image_logo,
                          image_thumb=EXCLUDED.image_thumb,tmdb_id=EXCLUDED.tmdb_id,parent_id=EXCLUDED.parent_id,
                          date_created=CASE WHEN dragonli_emby_movies.size IS DISTINCT FROM EXCLUDED.size
                            THEN NOW() ELSE dragonli_emby_movies.date_created END,
                          mtime=NOW(),isvalid=1''',
                        [(movie_id, *values) for movie_id, values, _ in movies],
                    )
                    sources = [source for _, _, entries in movies for source in entries]
                    if sources:
                        cursor.executemany(
                            '''INSERT INTO dragonli_emby_movie_sources
                            (id,movie_id,path,container,size,name,is_remote,supports_transcoding,
                             supports_direct_stream,supports_direct_play,ctime,mtime,isvalid)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),1)
                            ON CONFLICT (id) DO UPDATE SET
                              movie_id=EXCLUDED.movie_id,path=EXCLUDED.path,container=EXCLUDED.container,
                              size=EXCLUDED.size,name=EXCLUDED.name,is_remote=EXCLUDED.is_remote,
                              supports_transcoding=EXCLUDED.supports_transcoding,
                              supports_direct_stream=EXCLUDED.supports_direct_stream,
                              supports_direct_play=EXCLUDED.supports_direct_play,mtime=NOW(),isvalid=1''',
                            sources,
                        )
        created = sum(movie_id not in existing_ids for movie_id, _, _ in movies)
        source_count = sum(len(entries) for _, _, entries in movies)
        return {
            "created": created,
            "updated": len(movies) - created,
            "skipped": skipped,
            "sources": source_count,
            "source_skipped": source_skipped,
        }

    def series_path_targets(self) -> dict[str, dict[str, object]]:
        """Return existing Emby series directories keyed by their TMDB identity.

        The Emby-side name/category is the destination authority.  MoviePilot's
        organized name is only ever used as the transfer source hint because a
        later scrape can rename the very same title.
        """
        rows = self._all(
            '''SELECT s.id::text AS id,s.themoviedb,s.name,s.alias,l.name AS library_name
            FROM dragonli_emby_series s
            INNER JOIN dragonli_library_subfolders l ON l.seq=s.parent_id
            WHERE s.isvalid=1
              AND s.themoviedb IS NOT NULL AND btrim(s.themoviedb) <> ''
            ORDER BY s.themoviedb,s.id'''
        )
        targets: dict[str, dict[str, object]] = {}
        for row in rows:
            tmdb_id = _string(row.get("themoviedb"))
            library_name = _string(row.get("library_name"))
            if tmdb_id and library_name and _string(row.get("name")):
                targets[tmdb_id] = row
        return targets

    def series_cloud_path_targets(self) -> list[dict[str, object]]:
        """Return series with a manually supplied cloud source link.

        ``quark`` and ``alipan`` are maintained by the operator on an active
        tracking row. Archived records must never create transfer mappings.
        """
        return self._all(
            '''SELECT s.id::text AS id,s.name,s.alias,s.themoviedb,s.quark,s.alipan,
                      l.name AS library_name,n.name AS node_name
               FROM dragonli_emby_series s
               INNER JOIN dragonli_library_subfolders l ON l.seq=s.parent_id
               LEFT JOIN dragonli_storage_nodes n ON n.seq=l.node_id
               WHERE s.isvalid=1 AND s."update" IS TRUE
                 AND l.name IS NOT NULL AND btrim(l.name) <> ''
                 AND (
                    (s.quark IS NOT NULL AND btrim(s.quark) <> '')
                    OR (s.alipan IS NOT NULL AND btrim(s.alipan) <> '')
                 )
               ORDER BY l.name,s.name,s.id'''
        )

    def series_storage_nodes(self, series_ids: Iterable[str]) -> dict[str, str]:
        """Return the configured storage node for exact Emby series IDs."""
        ids = sorted({parsed for value in series_ids if (parsed := _positive_int(value)) is not None})
        if not ids:
            return {}
        rows = self._all(
            '''SELECT s.id::text AS id,n.name AS node_name
               FROM dragonli_emby_series s
               INNER JOIN dragonli_library_subfolders l ON l.seq=s.parent_id
               LEFT JOIN dragonli_storage_nodes n ON n.seq=l.node_id
               WHERE s.id = ANY(%s) AND s.isvalid=1''',
            (ids,),
        )
        return {
            _string(row.get("id")): _string(row.get("node_name"))
            for row in rows
            if _string(row.get("id")) and _string(row.get("node_name"))
        }

    def library_nodes(self) -> dict[str, str]:
        """Return the actual destination library's configured node, with legacy defaults."""
        rows = self._all(
            '''SELECT l.name AS library_name,n.name AS node_name,n.seq::text AS node_seq
            FROM dragonli_library_subfolders l
            LEFT JOIN dragonli_storage_nodes n ON n.seq=l.node_id
            WHERE l.name IS NOT NULL AND btrim(l.name) <> ''
            ORDER BY l.seq'''
        )
        nodes: dict[str, str] = {}
        for row in rows:
            library_name = _string(row.get("library_name"))
            if not library_name:
                continue
            node_name = _string(row.get("node_name")) or _string(row.get("node_seq"))
            nodes[library_name] = node_name or ("emby2" if library_name.startswith("动漫集") else "emby0")
        return nodes

    def canonical_storage_node(self, node_id: str) -> str:
        requested_node = node_id.strip()
        if not requested_node:
            return ""
        row = self._one(
            '''SELECT name FROM dragonli_storage_nodes
            WHERE name=%s OR seq::text=%s LIMIT 1''',
            (requested_node, requested_node),
        )
        return _string(row.get("name")) if row and _string(row.get("name")) else requested_node

    def list_libraries(self) -> list[dict[str, object]]:
        return self._all(
            '''SELECT DISTINCT l.name
            FROM dragonli_library_subfolders l
            WHERE l.name IS NOT NULL AND btrim(l.name) <> ''
              AND l.name NOT IN ('playlists','tmp','userplaylists')
              AND l.name NOT LIKE '%%电影%%'
            ORDER BY l.name'''
        )

    def list_series(
        self, page: int, size: int | None, query: str | None, tracking: bool | None = None,
        state: str | None = None,
    ) -> dict[str, object]:
        clauses, parameters = ["s.isvalid=1"], []
        if query:
            clauses.append("(s.name ILIKE %s OR s.index_name ILIKE %s)")
            parameters.extend((f"%{query}%", f"%{query}%"))
        if tracking is not None:
            clauses.append('s."update" IS TRUE' if tracking else 'COALESCE(s."update",FALSE) IS FALSE')
        if state == "today":
            clauses.append(f"s.next_update={_SHANGHAI_CURRENT_DATE}")
        elif state == "exception":
            # "Today" and "exception" deliberately overlap: a title can have
            # a scheduled episode today while already missing an aired episode.
            clauses.append(_EXCEPTION_CONDITION)
        where = " AND ".join(clauses)
        total = self._one(f"SELECT COUNT(*) AS count FROM dragonli_emby_series s WHERE {where}", parameters)
        tracked = self._one(
            'SELECT COUNT(*) AS count FROM dragonli_emby_series WHERE isvalid=1 AND "update" IS TRUE'
        )
        following_counts = self._one(
            f'''SELECT COUNT(*) FILTER (WHERE s.next_update={_SHANGHAI_CURRENT_DATE}) AS today,
            COUNT(*) FILTER (WHERE {_EXCEPTION_CONDITION}) AS exception
            FROM dragonli_emby_series s WHERE s.isvalid=1 AND s."update" IS TRUE'''
        ) or {}
        pagination = "" if size is None else " LIMIT %s OFFSET %s"
        row_parameters = parameters if size is None else [*parameters, size, (page - 1) * size]
        rows = self._all(
            f"""SELECT s.id::text AS id,s.name,s.server_id,s.date_created,s.season,s.season_number,s.lock_season,
            s.server_latest,s.official_latest,s.update_time,s.next_update,s.mtime,s.total,s."update" AS tracking,
            s.themoviedb,s.quark,s.alipan,s.alias,s.index_name,
            (s.next_update={_SHANGHAI_CURRENT_DATE}) AS is_today,
            ({_OVERDUE_CONDITION}) AS is_overdue,
            ({_MISSING_AIRED_CONDITION}) AS is_missing_aired,
            ({_STALE_METADATA_CONDITION}) AS is_metadata_stale,
            COALESCE(l.name,'—') AS library_name,COALESCE(n.name,'—') AS node_name,n.status AS node_status,
            COALESCE(e.episode_count,0) AS episode_count,COALESCE(e.latest_index,0) AS local_latest,
            e.latest_episode
            FROM dragonli_emby_series s
            LEFT JOIN dragonli_library_subfolders l ON l.seq=s.parent_id
            LEFT JOIN dragonli_storage_nodes n ON n.seq=l.node_id
            LEFT JOIN LATERAL (
              SELECT COUNT(DISTINCT index_number) AS episode_count,
                MAX(index_number) AS latest_index,MAX(date_created) AS latest_episode
              FROM dragonli_emby_episodes episode
              WHERE episode.isvalid=1 AND episode.series_id=s.id
                AND (COALESCE(s.lock_season,s.season_number) IS NULL
                  OR episode.parent_index_number=COALESCE(s.lock_season,s.season_number))
            ) e ON TRUE
            WHERE {where}
            ORDER BY s.index_name ASC NULLS LAST,s.name ASC{pagination}""",
            row_parameters,
        )
        return {
            "items": rows,
            "total": int(total["count"]) if total else 0,
            "tracked_total": int(tracked["count"]) if tracked else 0,
            "page": page,
            "page_size": size,
            "today_total": int(following_counts.get("today") or 0),
            "exception_total": int(following_counts.get("exception") or 0),
        }

    def set_series_tracking(self, series_id: int, tracking: bool) -> bool:
        existing = self._one(
            "SELECT id FROM dragonli_emby_series WHERE id=%s AND isvalid=1", (series_id,)
        )
        if existing is None:
            return False
        self._write(
            'UPDATE dragonli_emby_series SET "update"=%s,mtime=NOW() WHERE id=%s AND isvalid=1',
            (tracking, series_id),
        )
        return True

    def series_detail(self, series_id: int) -> dict[str, object] | None:
        return self._one(
            '''SELECT s.id::text AS id,l.name AS library_name,s."update" AS tracking,s.themoviedb,s.quark,s.alipan,s.alias,s.lock_season,
            s.index_name FROM dragonli_emby_series s
            LEFT JOIN dragonli_library_subfolders l ON l.seq=s.parent_id
            WHERE s.id=%s AND s.isvalid=1''',
            (series_id,),
        )

    def update_series_detail(self, series_id: int, detail: Mapping[str, object]) -> bool:
        current_library = self._one(
            '''SELECT s.parent_id,l.name AS library_name,l.node_id
            FROM dragonli_emby_series s
            LEFT JOIN dragonli_library_subfolders l ON l.seq=s.parent_id
            WHERE s.id=%s AND s.isvalid=1''',
            (series_id,),
        )
        if current_library is None:
            return False
        library_name = _nullable(detail.get("library_name"))
        current_library_name = _nullable(current_library.get("library_name"))

        # Library category names are intentionally shared by multiple storage nodes.
        # A settings save with an unchanged category must keep the exact parent_id;
        # resolving it again by name could silently move the series to another node.
        library_id: int | None = None
        if library_name and library_name != current_library_name:
            target_library = self._one(
                '''SELECT seq FROM dragonli_library_subfolders
                WHERE name=%s
                ORDER BY CASE WHEN node_id=%s THEN 0 ELSE 1 END,seq
                LIMIT 1''',
                (library_name, current_library.get("node_id")),
            )
            if target_library is None:
                return False
            library_id = int(target_library["seq"])
        self._write(
            '''UPDATE dragonli_emby_series SET "update"=%s,themoviedb=%s,quark=%s,alipan=%s,alias=%s,
            lock_season=%s,index_name=%s,parent_id=COALESCE(%s,parent_id),mtime=NOW() WHERE id=%s AND isvalid=1''',
            (
                bool(detail["tracking"]), _nullable(detail.get("themoviedb")), _nullable(detail.get("quark")),
                _nullable(detail.get("alipan")), _nullable(detail.get("alias")),
                _positive_int(detail.get("lock_season")), _nullable(detail.get("index_name")), library_id, series_id,
            ),
        )
        return True

    def episode_comparison(self, series_id: int) -> dict[str, object] | None:
        series = self._one(
            "SELECT id::text AS id,name,season_number,lock_season FROM dragonli_emby_series WHERE id=%s AND isvalid=1",
            (series_id,),
        )
        if series is None:
            return None
        season_number = _positive_int(series.get("lock_season")) or _positive_int(series.get("season_number")) or 1
        server_rows = self._all(
            '''SELECT index_number AS episode_number,name,date_created,path FROM dragonli_emby_episodes
            WHERE isvalid=1 AND series_id=%s AND parent_index_number=%s ORDER BY index_number''',
            (series_id, season_number),
        )
        tmdb_rows = self._all(
            '''SELECT episode_number,name,air_date,overview FROM dragonli_tmdb_episodes
            WHERE isvalid=1 AND tid=%s AND season_number=%s ORDER BY episode_number''',
            (series_id, season_number),
        )
        server = {_positive_int(row.get("episode_number")): row for row in server_rows if _positive_int(row.get("episode_number"))}
        tmdb = {_positive_int(row.get("episode_number")): row for row in tmdb_rows if _positive_int(row.get("episode_number"))}
        episodes = [{
            "episode_number": number, "server": server.get(number), "tmdb": tmdb.get(number),
            "has_server": number in server, "has_tmdb": number in tmdb,
        } for number in sorted(set(server) | set(tmdb))]
        return {"series": {"id": series["id"], "name": series["name"]}, "season_number": season_number, "comparison": episodes}

    def upsert_emby_series(self, payload: Mapping[str, object]) -> str:
        """Import an Emby series, updating an existing record when its TMDB ID matches."""
        series_id = _positive_int(payload.get("Id"))
        name = _string(payload.get("Name"))[:200]
        if series_id is None or not name:
            return "skipped"
        provider_ids = payload.get("ProviderIds")
        providers = provider_ids if isinstance(provider_ids, Mapping) else {}
        tmdb_id = next(
            (_string(providers.get(key)) for key in ("Tmdb", "TMDB", "tmdb") if _string(providers.get(key))),
            None,
        )
        production_year = _production_year(payload.get("ProductionYear"))
        index_prefix = _series_index_prefix(name)
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (94127104,))
                current = connection.execute(
                    'SELECT id,"update" FROM dragonli_emby_series WHERE id=%s FOR UPDATE', (series_id,)
                ).fetchone()
                matches: list[dict[str, object]] = []
                if tmdb_id:
                    matches = list(connection.execute(
                        '''SELECT id,"update" FROM dragonli_emby_series
                        WHERE isvalid=1 AND themoviedb=%s FOR UPDATE''', (tmdb_id,)
                    ).fetchall())
                inherited_tracking = any(row["update"] is True for row in matches)
                values = (
                    name,
                    _nullable(payload.get("ServerId")),
                    _positive_int(payload.get("ParentId")),
                    _nullable(payload.get("DateCreated")),
                    _bool(payload.get("IsFolder")) if isinstance(payload.get("IsFolder"), bool) else True,
                    _nullable(payload.get("Type")) or "Series",
                    production_year,
                    tmdb_id,
                    inherited_tracking,
                )
                incoming_parent_id = values[2]
                if current is not None:
                    connection.execute(
                        '''UPDATE dragonli_emby_series SET name=%s,server_id=%s,
                        parent_id=CASE WHEN "update" IS TRUE AND parent_id IS NOT NULL
                          AND COALESCE((SELECT name FROM dragonli_library_subfolders WHERE seq=parent_id),'')
                            <> COALESCE((SELECT name FROM dragonli_library_subfolders WHERE seq=%s),'')
                          THEN parent_id ELSE %s END,date_created=%s,
                        is_folder=%s,"type"=%s,alias=COALESCE(%s,alias),themoviedb=COALESCE(%s,themoviedb),
                        "update"=("update" IS TRUE OR %s),
                        index_name=CASE WHEN NULLIF(BTRIM(index_name),'') IS NULL OR index_name=name
                            THEN %s ELSE index_name END,
                        mtime=NOW(),isvalid=1 WHERE id=%s''',
                        (values[0], values[1], incoming_parent_id, incoming_parent_id,
                         *values[3:], index_prefix, series_id),
                    )
                    action = "updated"
                else:
                    matched_series = next((row for row in matches if int(row["id"]) != series_id), None)
                    if matched_series is not None:
                        previous_series_id = int(matched_series["id"])
                        connection.execute(
                            '''UPDATE dragonli_emby_series SET id=%s,name=%s,server_id=%s,
                            parent_id=CASE WHEN "update" IS TRUE AND parent_id IS NOT NULL
                              AND COALESCE((SELECT name FROM dragonli_library_subfolders WHERE seq=parent_id),'')
                                <> COALESCE((SELECT name FROM dragonli_library_subfolders WHERE seq=%s),'')
                              THEN parent_id ELSE %s END,date_created=%s,
                            is_folder=%s,"type"=%s,alias=COALESCE(%s,alias),themoviedb=%s,
                            "update"=("update" IS TRUE OR %s),
                            index_name=CASE WHEN NULLIF(BTRIM(index_name),'') IS NULL OR index_name=name
                                THEN %s ELSE index_name END,
                            mtime=NOW(),isvalid=1 WHERE id=%s''',
                            (series_id, values[0], values[1], incoming_parent_id, incoming_parent_id,
                             *values[3:8], inherited_tracking, index_prefix, previous_series_id),
                        )
                        # The legacy schema uses the series primary key as the Emby item ID.
                        # Move historical episode and TMDB tracking rows to that new item ID too.
                        connection.execute(
                            "UPDATE dragonli_emby_episodes SET series_id=%s WHERE series_id=%s",
                            (series_id, previous_series_id),
                        )
                        connection.execute(
                            "UPDATE dragonli_tmdb_episodes SET tid=%s WHERE tid=%s",
                            (series_id, previous_series_id),
                        )
                        action = "promoted"
                    else:
                        connection.execute(
                            '''INSERT INTO dragonli_emby_series
                            (id,name,server_id,parent_id,date_created,is_folder,"type",alias,ctime,mtime,isvalid,"update",themoviedb,index_name)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),1,%s,%s,%s)''',
                            (series_id, *values[:7], inherited_tracking, tmdb_id, index_prefix),
                        )
                        action = "created"
                if tmdb_id:
                    # TMDB identifies a single TV work.  A matching record was updated above;
                    # archive only any additional historical duplicates without deleting them.
                    connection.execute(
                        "UPDATE dragonli_emby_series SET isvalid=0,mtime=NOW() "
                        "WHERE themoviedb=%s AND id != %s AND isvalid=1",
                        (tmdb_id, series_id),
                    )
        return action

    def upsert_emby_episodes(self, payloads: list[Mapping[str, object]]) -> dict[str, int]:
        synced = skipped = 0
        series_ids: set[int] = set()
        rows: list[tuple[object, ...]] = []
        for payload in payloads:
            episode_id = _positive_int(payload.get("Id"))
            series_id = _positive_int(payload.get("SeriesId"))
            name = _string(payload.get("Name"))[:500]
            if episode_id is None or series_id is None or not name:
                skipped += 1
                continue
            rows.append((
                episode_id, series_id, _nullable(payload.get("SeriesName")),
                _non_negative_int(payload.get("ParentIndexNumber")),
                _non_negative_int(payload.get("IndexNumber")), name,
                _nullable(payload.get("DateCreated")), _nullable(payload.get("Path")),
            ))
            series_ids.add(series_id)
        if not rows:
            return {"synced": 0, "skipped": skipped}
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.executemany(
                        '''INSERT INTO dragonli_emby_episodes
                        (id,series_id,series_name,parent_index_number,index_number,name,date_created,path,isvalid)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)
                        ON CONFLICT (id) DO UPDATE SET
                          series_id=EXCLUDED.series_id,series_name=EXCLUDED.series_name,
                          parent_index_number=EXCLUDED.parent_index_number,index_number=EXCLUDED.index_number,
                          name=EXCLUDED.name,date_created=EXCLUDED.date_created,path=EXCLUDED.path,isvalid=1''',
                        rows,
                    )
                connection.execute(
                    '''UPDATE dragonli_emby_series AS series SET server_latest=episode.latest,
                      mtime=NOW()
                    FROM (
                      SELECT series_id,MAX(index_number) AS latest
                      FROM dragonli_emby_episodes
                      WHERE isvalid=1 AND series_id = ANY(%s)
                      GROUP BY series_id
                    ) AS episode
                    WHERE series.id=episode.series_id''',
                    (list(series_ids),),
                )
        return {"synced": len(rows), "skipped": skipped}

    def tracked_emby_series_ids(self) -> list[int]:
        rows = self._all(
            '''SELECT id FROM dragonli_emby_series
            WHERE isvalid=1 AND "update" IS TRUE AND id > 0 ORDER BY id'''
        )
        return [int(row["id"]) for row in rows]

    def list_tracked_series(self) -> list[dict[str, object]]:
        return self._all(
            '''SELECT s.id::text AS id,s.name,s.themoviedb,s.lock_season,s.season_number,
              latest_local.parent_index_number AS local_season_number
            FROM dragonli_emby_series s
            LEFT JOIN LATERAL (
              SELECT e.parent_index_number
              FROM dragonli_emby_episodes e
              WHERE e.series_id=s.id AND e.isvalid=1 AND e.parent_index_number > 0
              ORDER BY e.date_created DESC NULLS LAST,e.parent_index_number DESC
              LIMIT 1
            ) latest_local ON TRUE
            WHERE s.isvalid=1 AND s."update" IS TRUE
            ORDER BY s.next_update ASC NULLS LAST,s.id ASC'''
        )

    def tracked_series(self, series_id: int) -> dict[str, object] | None:
        return self._one(
            '''SELECT s.id::text AS id,s.name,s.themoviedb,s.lock_season,s.season_number,
              latest_local.parent_index_number AS local_season_number
            FROM dragonli_emby_series s
            LEFT JOIN LATERAL (
              SELECT e.parent_index_number
              FROM dragonli_emby_episodes e
              WHERE e.series_id=s.id AND e.isvalid=1 AND e.parent_index_number > 0
              ORDER BY e.date_created DESC NULLS LAST,e.parent_index_number DESC
              LIMIT 1
            ) latest_local ON TRUE
            WHERE s.id=%s AND s.isvalid=1 AND s."update" IS TRUE''',
            (series_id,),
        )

    def save_tracking_metadata(
        self,
        series_id: int,
        season_name: str | None,
        season_number: int | None,
        official_latest: int | None,
        next_update: str | None,
        total: int | None,
    ) -> None:
        if season_number is None:
            local = self._one(
                "SELECT MAX(index_number) AS latest FROM dragonli_emby_episodes WHERE series_id=%s AND isvalid=1",
                (series_id,),
            )
        else:
            local = self._one(
                """SELECT MAX(index_number) AS latest FROM dragonli_emby_episodes
                WHERE series_id=%s AND parent_index_number=%s AND isvalid=1""",
                (series_id, season_number),
            )
        server_latest = int(local["latest"]) if local and local["latest"] is not None else None
        self._write(
            f'''UPDATE dragonli_emby_series SET
              season=%s,season_number=%s,server_latest=%s,official_latest=%s,next_update=%s,
              total=%s,update_time={_SHANGHAI_CURRENT_DATE},mtime=NOW()
              WHERE id=%s AND isvalid=1 AND "update" IS TRUE''',
            (
                season_name,
                season_number,
                server_latest,
                official_latest,
                next_update,
                total,
                series_id,
            ),
        )

    def upsert_tmdb_episodes(self, series_id: int, payload: Mapping[str, object]) -> dict[str, int]:
        """Persist TMDB's selected season exactly as the legacy Themoviedb cron did."""
        episodes = payload.get("episodes")
        if not isinstance(episodes, list):
            return {"synced": 0, "skipped": 0}
        rows: list[tuple[object, ...]] = []
        skipped = 0
        for episode in episodes:
            if not isinstance(episode, Mapping):
                skipped += 1
                continue
            episode_id = _positive_int(episode.get("id"))
            show_id = _positive_int(episode.get("show_id"))
            season_number = _positive_int(episode.get("season_number"))
            episode_number = _positive_int(episode.get("episode_number"))
            name = _string(episode.get("name"))
            if None in (episode_id, show_id, season_number, episode_number) or not name:
                skipped += 1
                continue
            rows.append((
                episode_id, series_id, show_id, season_number, episode_number, name,
                _nullable(episode.get("overview")), _nullable(episode.get("air_date")),
                _nullable(episode.get("episode_type")), _nullable(episode.get("production_code")),
                _non_negative_int(episode.get("runtime")), _nullable(episode.get("still_path")),
                _decimal(episode.get("vote_average")), _non_negative_int(episode.get("vote_count")),
            ))
        if not rows:
            return {"synced": 0, "skipped": skipped}
        current_season = rows[0][3]
        incoming_ids = [row[0] for row in rows]
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.executemany(
                        '''INSERT INTO dragonli_tmdb_episodes
                        (id,tid,show_id,season_number,episode_number,name,overview,air_date,episode_type,
                         production_code,runtime,still_path,vote_average,vote_count,ctime,mtime,isvalid)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),1)
                        ON CONFLICT (id) DO UPDATE SET
                          tid=EXCLUDED.tid,show_id=EXCLUDED.show_id,season_number=EXCLUDED.season_number,
                          episode_number=EXCLUDED.episode_number,name=EXCLUDED.name,overview=EXCLUDED.overview,
                          air_date=EXCLUDED.air_date,episode_type=EXCLUDED.episode_type,
                          production_code=EXCLUDED.production_code,runtime=EXCLUDED.runtime,
                          still_path=EXCLUDED.still_path,vote_average=EXCLUDED.vote_average,
                          vote_count=EXCLUDED.vote_count,mtime=NOW(),isvalid=1''',
                        rows,
                    )
                # Metadata providers occasionally correct a season's episode list.
                # Keep the legacy rows for audit purposes, but exclude entries
                # no longer returned by the current TMDB season response.
                connection.execute(
                    '''UPDATE dragonli_tmdb_episodes SET isvalid=0,mtime=NOW()
                    WHERE tid=%s AND season_number=%s AND isvalid=1
                      AND NOT (id = ANY(%s))''',
                    (series_id, current_season, incoming_ids),
                )
        return {"synced": len(rows), "skipped": skipped}

    def import_moviepilot_series(self, subscriptions: Mapping[str, str]) -> dict[str, int]:
        """Enable existing series and add missing MoviePilot subscriptions without DDL.

        The legacy table has an integer primary key but no identity/sequence. Imported records
        therefore receive unused negative IDs so they cannot collide with Emby's positive IDs.
        """
        if not subscriptions:
            return {"matched": 0, "enabled": 0, "created": 0}
        identifiers = sorted(subscriptions)
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            with connection.transaction():
                # Keep generated negative IDs unique when another sync process is active.
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (94127104,))
                matched_row = connection.execute(
                    """SELECT COUNT(*) AS count FROM dragonli_emby_series
                    WHERE isvalid=1 AND themoviedb = ANY(%s)""",
                    (identifiers,),
                ).fetchone()
                enabled_row = connection.execute(
                    '''SELECT COUNT(*) AS count FROM dragonli_emby_series
                    WHERE isvalid=1 AND themoviedb = ANY(%s) AND "update" IS NOT TRUE''',
                    (identifiers,),
                ).fetchone()
                existing_rows = connection.execute(
                    "SELECT id,themoviedb,name,index_name FROM dragonli_emby_series "
                    "WHERE isvalid=1 AND themoviedb = ANY(%s)",
                    (identifiers,),
                ).fetchall()
                existing = {str(row["themoviedb"]) for row in existing_rows if row["themoviedb"] is not None}
                for row in existing_rows:
                    name = _string(row.get("name"))
                    index_name = _string(row.get("index_name"))
                    if name and (not index_name or index_name == name):
                        connection.execute(
                            "UPDATE dragonli_emby_series SET index_name=%s,mtime=NOW() WHERE id=%s",
                            (_series_index_prefix(name), row["id"]),
                        )
                connection.execute(
                    '''UPDATE dragonli_emby_series SET "update"=TRUE,mtime=NOW()
                    WHERE isvalid=1 AND themoviedb = ANY(%s) AND "update" IS NOT TRUE''',
                    (identifiers,),
                )
                next_id_row = connection.execute(
                    "SELECT COALESCE(MIN(id), 0) - 1 AS next_id FROM dragonli_emby_series"
                ).fetchone()
                next_id = int(next_id_row["next_id"]) if next_id_row else -1
                created = 0
                for tmdb_id in identifiers:
                    if tmdb_id in existing:
                        continue
                    connection.execute(
                        '''INSERT INTO dragonli_emby_series
                        (id,name,date_created,is_folder,"type",ctime,mtime,isvalid,"update",themoviedb,index_name)
                        VALUES (%s,%s,NOW(),TRUE,'Series',NOW(),NOW(),1,TRUE,%s,%s)''',
                        (next_id, subscriptions[tmdb_id], tmdb_id, _series_index_prefix(subscriptions[tmdb_id])),
                    )
                    next_id -= 1
                    created += 1
        return {
            "matched": int(matched_row["count"]) if matched_row else 0,
            "enabled": int(enabled_row["count"]) if enabled_row else 0,
            "created": created,
        }

    def enable_moviepilot_series_by_name_year(
        self, subscriptions: Iterable[tuple[str, str]],
    ) -> dict[str, int]:
        """Enable only unambiguous non-TMDB MoviePilot subscriptions.

        MoviePilot can subscribe through Douban without exposing a TMDB ID.  We
        never create a tracking row from that incomplete identity.  An existing
        Emby row may be enabled only when both its title and its scraper year
        (stored in ``alias``) are exact, and the pair identifies one row.
        """
        identities = sorted({(name.strip(), year.strip()) for name, year in subscriptions if name and year})
        if not identities:
            return {"matched": 0, "enabled": 0}
        matched = enabled = 0
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (94127104,))
                for name, year in identities:
                    rows = connection.execute(
                        """SELECT id,\"update\" FROM dragonli_emby_series
                        WHERE isvalid=1 AND name=%s AND alias=%s""",
                        (name, year),
                    ).fetchall()
                    if len(rows) != 1:
                        continue
                    matched += 1
                    row = rows[0]
                    if row["update"] is True:
                        continue
                    connection.execute(
                        '''UPDATE dragonli_emby_series SET "update"=TRUE,mtime=NOW() WHERE id=%s''',
                        (row["id"],),
                    )
                    enabled += 1
        return {"matched": matched, "enabled": enabled}

    def _all(self, sql: str, parameters: list[object] | tuple[object, ...] = ()) -> list[dict[str, object]]:
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            return list(connection.execute(sql, parameters).fetchall())

    def _one(self, sql: str, parameters: list[object] | tuple[object, ...] = ()) -> dict[str, object] | None:
        with closing(psycopg.connect(self._database_url, row_factory=dict_row)) as connection:
            return connection.execute(sql, parameters).fetchone()

    def _write(self, sql: str, parameters: tuple[object, ...]) -> None:
        with closing(psycopg.connect(self._database_url)) as connection:
            connection.execute(sql, parameters)
            connection.commit()


def _session_values(session: WatchSession) -> tuple[object, ...]:
    return (session.user_id,session.username,session.item_id,session.item_name,session.item_type,
            session.play_start_time,session.play_position,session.play_progress,session.play_position,
            session.total_duration,session.ip_address,session.device_name,session.client_name,session.session_id)


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _nullable(value: object) -> str | None:
    return _string(value) or None


def _traffic_values(row: Mapping[str, object], *, include_rate: bool = False, include_date: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "rx_bytes": int(row.get("rx_bytes") or 0),
        "tx_bytes": int(row.get("tx_bytes") or 0),
        "total_bytes": int(row.get("total_bytes") or 0),
    }
    if include_rate:
        result["avg_rate"] = float(row.get("avg_rate") or 0)
        result["updated_at"] = row.get("updated_at")
    if include_date:
        result["date"] = row.get("date")
    return result


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_negative_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _decimal(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _production_year(value: object) -> str | None:
    year = _positive_int(value)
    return str(year) if year is not None and 1800 <= year <= 3000 else None


def _series_index_prefix(name: str) -> str:
    initials = lazy_pinyin(name, style=Style.FIRST_LETTER, strict=False, errors=lambda _: [])
    for initial in initials:
        if initial and initial[0].isalpha():
            return initial[0].upper()
    for character in name:
        if character.isascii() and character.isalpha():
            return character.upper()
    return "#"
