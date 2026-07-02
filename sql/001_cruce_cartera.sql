-- Tablas mirror de referencia (se reemplazan por completo en cada sync_cartera.py)
-- No llevan primary key en la columna de cruce: el mismo email/referencia puede
-- repetirse en el Excel (pagos repetidos del mismo cliente). El lookup replica
-- BUSCARV de Excel: toma la primera coincidencia, de arriba hacia abajo.

create table if not exists cartera_inscrip (
  id             bigserial primary key,
  numero_id      text,
  id_inscripcion text
);
create index if not exists idx_cartera_inscrip_numero_id on cartera_inscrip (numero_id);

create table if not exists cartera_ingresos_bancolombia_2576 (
  id           bigserial primary key,
  referencia_1 text,
  incp         text
);
create index if not exists idx_cartera_ing_bc2576_ref1 on cartera_ingresos_bancolombia_2576 (referencia_1);

create table if not exists cartera_ingresos_wompi (
  id      bigserial primary key,
  email   text,
  inscrip text
);
create index if not exists idx_cartera_ing_wompi_email on cartera_ingresos_wompi (email);

create table if not exists cartera_ingresos_stripe_usa (
  id            bigserial primary key,
  email_cliente text,
  incp          text
);
create index if not exists idx_cartera_ing_stripe_email on cartera_ingresos_stripe_usa (email_cliente);

-- Resultado del cruce. Columnas 0-9 son passthrough de consolidated_transactions,
-- 10-11 (incp, correo_2) ya están implementadas; 12-19 quedan NULL hasta definirlas.
create table if not exists cruce_cartera (
  matching_key        text primary key,
  val                 text,
  identification       text,
  payment_date         date,
  transaction_code_1  text,
  transaction_code_2  text,
  email                text,
  payment_method       text,
  program              text,
  phone                text,
  payment_amount       numeric,
  incp                 text,
  correo_2             text,
  cruce                text,
  nombre               text,
  metodo_de_pago       text,
  ci                   text,
  nemonico             text,
  clasificacion        text,
  convocatoria         text,
  validacion           text,
  updated_at           timestamptz not null default now()
);
