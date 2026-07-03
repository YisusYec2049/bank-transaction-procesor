-- Estado del cruce por fila, para no recalcular/sobrescribir para siempre y
-- para poder marcar excepciones que requieren revisión humana (no se resuelven
-- solas): correos/identificaciones con más de un resultado posible en las
-- hojas de referencia (ej. una pareja que paga dos inscripciones con su mismo
-- correo), o simplemente sin ningún resultado.
--
-- estado_cruce:
--   'pendiente'        -> nueva o excepción sin revisar. cruzar.py la sigue
--                         recalculando cada corrida.
--   'cruzado'          -> cruce limpio automático, o corregido a mano en
--                         financial-platform. cruzar.py no la vuelve a tocar.
--   'no_identificable' -> revisada a mano y descartada (no se puede
--                         identificar). cruzar.py tampoco la vuelve a tocar.
--
-- excepcion_motivo: 'sin_cruce' | 'cruce_ambiguo' | NULL (cruce limpio).

alter table cruce_cartera
  add column if not exists estado_cruce text not null default 'pendiente',
  add column if not exists excepcion_motivo text;

alter table cruce_cartera
  drop constraint if exists chk_cruce_cartera_estado_cruce;
alter table cruce_cartera
  add constraint chk_cruce_cartera_estado_cruce
  check (estado_cruce in ('pendiente', 'cruzado', 'no_identificable'));

alter table cruce_cartera
  drop constraint if exists chk_cruce_cartera_excepcion_motivo;
alter table cruce_cartera
  add constraint chk_cruce_cartera_excepcion_motivo
  check (excepcion_motivo is null or excepcion_motivo in ('sin_cruce', 'cruce_ambiguo'));

create index if not exists idx_cruce_cartera_estado on cruce_cartera (estado_cruce);
