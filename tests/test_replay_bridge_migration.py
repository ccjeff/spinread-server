"""Exercise the additive migration against existing persisted records in SQLite."""
import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text, inspect


def test_bridge_migration_preserves_video_training_and_quiz_rows(tmp_path):
    path=Path(__file__).parents[1]/'alembic/versions/0009_replay_training_bridge.py'
    spec=importlib.util.spec_from_file_location('bridge_migration',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    with engine.begin() as conn:
        for table in ('users','timelines','artifacts','review_requests'):
            conn.exec_driver_sql(f'CREATE TABLE {table} (id TEXT PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE videos (id TEXT PRIMARY KEY, filename TEXT, state TEXT)')
        conn.exec_driver_sql("INSERT INTO videos VALUES ('v','existing.mov','READY')")
        conn.exec_driver_sql('CREATE TABLE quiz_items (id TEXT PRIMARY KEY, version INTEGER, answer_basis JSON)')
        conn.exec_driver_sql("INSERT INTO quiz_items VALUES ('q',3,'{}')")
        conn.exec_driver_sql('CREATE TABLE quiz_attempts (id TEXT PRIMARY KEY, quiz_item_id TEXT, answer JSON)')
        conn.exec_driver_sql("INSERT INTO quiz_attempts VALUES ('a','q','{\"spin\":\"UNKNOWN\"}')")
        conn.exec_driver_sql('CREATE TABLE plan_items (id TEXT PRIMARY KEY, title TEXT, version INTEGER, player_note TEXT)')
        conn.exec_driver_sql("INSERT INTO plan_items VALUES ('p','Existing coach task',4,'Two sessions completed')")
        conn.exec_driver_sql('CREATE TABLE plan_item_retests (id TEXT PRIMARY KEY, plan_item_version INTEGER, result JSON)')
        conn.exec_driver_sql("INSERT INTO plan_item_retests VALUES ('rt',2,'{\"comparable\":false}')")
        before={t:list(conn.execute(text(f'SELECT * FROM {t}'))) for t in
                ('videos','quiz_items','quiz_attempts','plan_item_retests')}
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        for table,rows in before.items():
            assert list(conn.execute(text(f'SELECT * FROM {table}')))==rows
        row=conn.execute(text('SELECT * FROM plan_items')).one()
        assert tuple(row)==('p','Existing coach task',4,'Two sessions completed',None)
        tables=inspect(conn).get_table_names()
        assert 'quiz_reference_answers' in tables and 'replay_analyses' in tables
    engine.dispose()
