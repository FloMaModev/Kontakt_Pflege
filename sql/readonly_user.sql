-- Nur-Lese-Benutzer für den E-Rechnungs-Check anlegen.
-- Als Datenbank-Administrator in der ODOO-Datenbank ausführen, z. B.:
--   docker exec -i <odoo-db-container> psql -U odoo -d <odoo-datenbank> < sql/readonly_user.sql
-- Passwort unten vorher ändern (und gleich in .env als PGPASSWORD eintragen).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erechnung_ro') THEN
    CREATE ROLE erechnung_ro LOGIN PASSWORD 'bitte-aendern';
  END IF;
END $$;

-- Jede Sitzung dieses Benutzers ist automatisch schreibgeschützt
ALTER ROLE erechnung_ro SET default_transaction_read_only = on;
ALTER ROLE erechnung_ro CONNECTION LIMIT 5;

-- Lesen erlauben, sonst nichts
DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO erechnung_ro', current_database()); END $$;
GRANT USAGE ON SCHEMA public TO erechnung_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO erechnung_ro;
-- auch für Tabellen, die Odoo später (z. B. bei Modul-Installationen) anlegt.
-- Gilt für den Eigentümer der Odoo-Tabellen (der Odoo-Datenbankbenutzer).
DO $$ BEGIN
  EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO erechnung_ro',
                 (SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = 'res_partner'));
END $$;
