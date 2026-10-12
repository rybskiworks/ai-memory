//! Fork schema changes use a separate history so upstream versions stay intact.

use rusqlite::{Connection, OptionalExtension, params};

use crate::error::{StoreError, StoreResult};

refinery::embed_migrations!("fork-migrations");

const PRIME_SQL: &str = include_str!("../fork-migrations/V01__sessions_prime_agent_kind.sql");
const ABSTRACT_SQL: &str = include_str!("../migrations/V61__page_abstract_embeddings.sql");

pub(crate) fn run(conn: &mut Connection) -> StoreResult<()> {
    migrations::runner()
        .set_migration_table_name("refinery_fork_schema_history")
        .run(conn)?;
    Ok(())
}

/// The deployed fork used V61 for Prime, before upstream allocated V61 to
/// abstract embeddings. Only that exact released migration can be adopted.
/// Add the missing upstream schema and replace its history row atomically;
/// the independent fork runner then records the retained Prime constraint.
pub(crate) fn adopt_legacy(conn: &mut Connection, supported: u32) -> StoreResult<()> {
    let history_exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' \
         AND name = 'refinery_schema_history')",
        [],
        |row| row.get(0),
    )?;
    if !history_exists {
        return Ok(());
    }
    let legacy: Option<(String, String)> = conn
        .query_row(
            "SELECT name, checksum FROM refinery_schema_history WHERE version = 61",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let Some((name, checksum)) = legacy else {
        return Ok(());
    };
    if name != "sessions_prime_agent_kind" {
        return Ok(());
    }
    let expected = refinery::Migration::unapplied("V61__sessions_prime_agent_kind", PRIME_SQL)?;
    if checksum != expected.checksum().to_string() {
        return Err(StoreError::ForkMigrationMismatch);
    }
    // Do not alter a database from a newer binary before the upstream runner
    // has a chance to reject it.
    let ahead: Option<(u32, String)> = conn
        .query_row(
            "SELECT version, name FROM refinery_schema_history WHERE version > ?1 \
             ORDER BY version DESC LIMIT 1",
            [supported],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    if let Some((version, name)) = ahead {
        return Err(StoreError::DataSchemaAhead {
            applied: format!("V{version} ({name})"),
            supported,
        });
    }
    let upstream = refinery::Migration::unapplied("V61__page_abstract_embeddings", ABSTRACT_SQL)?;
    let tx = conn.transaction()?;
    tx.execute_batch(ABSTRACT_SQL)?;
    tx.execute(
        "UPDATE refinery_schema_history SET name = ?1, checksum = ?2 WHERE version = 61",
        params![upstream.name(), upstream.checksum().to_string()],
    )?;
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ai_memory_core::{AgentKind, NewSession, SessionId};

    type TestResult = Result<(), Box<dyn std::error::Error>>;

    fn legacy_store() -> Result<Connection, Box<dyn std::error::Error>> {
        let mut conn = Connection::open_in_memory()?;
        crate::migrations::run_to(&mut conn, 60)?;
        let legacy = refinery::Migration::unapplied("V61__sessions_prime_agent_kind", PRIME_SQL)?;
        refinery::Runner::new(&[legacy])
            .set_abort_missing(false)
            .run(&mut conn)?;
        Ok(conn)
    }

    fn seed_session(
        conn: &mut Connection,
        agent_kind: AgentKind,
    ) -> Result<SessionId, Box<dyn std::error::Error>> {
        let workspace_id = crate::ops::get_or_create_workspace(conn, "upgrade")?;
        let project_id = crate::ops::get_or_create_project(conn, &workspace_id, "retained", None)?;
        let id = SessionId::new();
        crate::ops::begin_session(
            conn,
            &NewSession {
                id,
                workspace_id,
                project_id,
                agent_kind,
                cwd: Some("/repo".into()),
                actor_user: Some("user:alice".into()),
            },
        )?;
        conn.execute(
            "INSERT INTO observations (id, session_id, workspace_id, project_id, kind, title, body, created_at) \
             VALUES (randomblob(16), ?1, ?2, ?3, 'user-prompt', 'retained', 'upgrade canary', 1)",
            params![id.as_bytes(), workspace_id.as_bytes(), project_id.as_bytes()],
        )?;
        Ok(id)
    }

    #[test]
    fn legacy_prime_upgrade_preserves_data_and_is_repeatable() -> TestResult {
        let mut conn = legacy_store()?;
        let id = seed_session(&mut conn, AgentKind::PrimeAgent)?;
        conn.pragma_update(None, "foreign_keys", "OFF")?;
        crate::migrations::run(&mut conn)?;
        crate::migrations::run(&mut conn)?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        let preserved: (String, String, String) = conn.query_row(
            "SELECT s.agent_kind, s.actor_user, o.body FROM sessions s JOIN observations o \
             ON o.session_id = s.id WHERE s.id = ?1",
            [id.as_bytes()],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
        assert_eq!(
            preserved,
            (
                "prime-agent".into(),
                "user:alice".into(),
                "upgrade canary".into()
            )
        );
        let v61: String = conn.query_row(
            "SELECT name FROM refinery_schema_history WHERE version = 61",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(v61, "page_abstract_embeddings");
        let forks: i64 = conn.query_row(
            "SELECT COUNT(*) FROM refinery_fork_schema_history",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(forks, 1);
        let violations: i64 =
            conn.query_row("SELECT COUNT(*) FROM pragma_foreign_key_check", [], |row| {
                row.get(0)
            })?;
        assert_eq!(violations, 0);
        conn.execute("DELETE FROM page_abstract_embeddings", [])?;
        seed_session(&mut conn, AgentKind::PrimeAgent)?;
        Ok(())
    }

    #[test]
    fn fresh_and_upstream_stores_gain_prime_support() -> TestResult {
        for upstream in [false, true] {
            let mut conn = Connection::open_in_memory()?;
            if upstream {
                crate::migrations::run_to(&mut conn, 66)?;
                seed_session(&mut conn, AgentKind::Pi)?;
            }
            conn.pragma_update(None, "foreign_keys", "OFF")?;
            crate::migrations::run(&mut conn)?;
            conn.pragma_update(None, "foreign_keys", "ON")?;
            seed_session(&mut conn, AgentKind::PrimeAgent)?;
            let violations: i64 =
                conn.query_row("SELECT COUNT(*) FROM pragma_foreign_key_check", [], |row| {
                    row.get(0)
                })?;
            assert_eq!(violations, 0);
        }
        Ok(())
    }

    #[test]
    fn unknown_legacy_checksum_is_rejected_without_schema_changes() -> TestResult {
        let mut conn = legacy_store()?;
        conn.execute(
            "UPDATE refinery_schema_history SET checksum = '0' WHERE version = 61",
            [],
        )?;
        assert!(matches!(
            crate::migrations::run(&mut conn),
            Err(StoreError::ForkMigrationMismatch)
        ));
        let name: String = conn.query_row(
            "SELECT name FROM refinery_schema_history WHERE version = 61",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(name, "sessions_prime_agent_kind");
        Ok(())
    }

    #[test]
    fn failed_adoption_keeps_legacy_history_for_retry() -> TestResult {
        let mut conn = legacy_store()?;
        conn.execute(
            "CREATE INDEX idx_page_abstract_embeddings_provider ON sessions(started_at)",
            [],
        )?;
        assert!(matches!(
            crate::migrations::run(&mut conn),
            Err(StoreError::Sqlite(_))
        ));
        let tables: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'page_abstract_embeddings'",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(tables, 0);
        let name: String = conn.query_row(
            "SELECT name FROM refinery_schema_history WHERE version = 61",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(name, "sessions_prime_agent_kind");
        conn.execute("DROP INDEX idx_page_abstract_embeddings_provider", [])?;
        conn.pragma_update(None, "foreign_keys", "OFF")?;
        crate::migrations::run(&mut conn)?;
        Ok(())
    }

    #[test]
    fn newer_legacy_store_is_rejected_before_adoption() -> TestResult {
        let mut conn = legacy_store()?;
        conn.execute("INSERT INTO refinery_schema_history VALUES (100, 'future', '2026-09-22T00:00:00Z', '0')", [])?;
        assert!(matches!(
            crate::migrations::run(&mut conn),
            Err(StoreError::DataSchemaAhead { .. })
        ));
        let name: String = conn.query_row(
            "SELECT name FROM refinery_schema_history WHERE version = 61",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(name, "sessions_prime_agent_kind");
        Ok(())
    }
}
