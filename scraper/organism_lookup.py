"""Static ``(id_inciso, id_ue)`` → organism name lookup.

Replaces the RSS feed title-parsing that previously provided the
``organism`` field on :class:`scraper.normalizer.JoinedRecord` records. The
government's ``<unidades-ejecutoras>`` data is a static identifier-to-name
map, so encoding it as a Python dictionary makes the lookup a pure
function — no I/O, no parsing, no rate limiting.

The table is sourced from the official government codiguera. New
combinations will appear whenever the government reorganises an
organismo; the operator must update this table when that happens. The
``resolve_organism`` function never raises on a missing key — it logs a
warning and returns ``"Desconocido ({id_inciso}-{id_ue})"`` so a
partial mapping does not block ingestion (see ``organism-lookup`` spec,
"Unmapped pair returns Desconocido fallback" scenario).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Prefix used for the fallback string returned when ``(id_inciso, id_ue)``
# is not present in :data:`ORGANISM_MAP`. Exported so callers can identify
# the fallback without coupling to the literal Spanish text.
UNKNOWN_ORGANISM_PREFIX = "Desconocido"

# ---------------------------------------------------------------------------
# Static mapping — official government codiguera
# ---------------------------------------------------------------------------
# Key: (id_inciso, id_ue) as strings to match the raw XML attribute values.
# Value: full organism name as displayed in the UI.
#
# Source: <unidades-ejecutoras> XML from the procurement system.
# Updating this dict is the only maintenance the module needs — no schema
# migration, no settings, no I/O.
# ---------------------------------------------------------------------------
ORGANISM_MAP: dict[tuple[str, str], str] = {
    # Inciso 1 — Poder Legislativo
    ("1", "1"): "Cámara de Senadores",
    ("1", "2"): "Cámara de Representantes",
    ("1", "3"): "Comisión Administrativa del Poder Legislativo",
    # Inciso 2 — Presidencia de la República
    ("2", "1"): "Presidencia de la República y Unidades Dependientes",
    ("2", "3"): "Casa Militar",
    ("2", "4"): "Oficina de Planeamiento y Presupuesto",
    ("2", "5"): "NO VIGENTE - Dirección de Proyectos de Desarrollo",
    ("2", "6"): "Unidad Reguladora de Serv. de Energía y Agua-URSEA",
    ("2", "7"): "Instituto Nacional de EstadÍstica",
    ("2", "8"): "Oficina Nacional del Servicio Civil",
    ("2", "9"): "Unidad Reguladora de Servic.de Comunicaciones-URSEC",
    ("2", "10"): "Agen.p/Des.del Gob.de Gest.Electr.y Soc.Inform.y del Conoc.",
    ("2", "11"): "Secretaría Nacional del Deporte",
    # Inciso 3 — Ministerio de Defensa Nacional
    ("3", "1"): "Dirección General de Secretaría de Estado",
    ("3", "3"): "Estado Mayor de la Defensa",
    ("3", "4"): "Comando General del Ejército",
    ("3", "18"): "Comando General de la Armada",
    ("3", "23"): "Comando General de la Fuerza Aérea",
    ("3", "30"): "*NO VALE Ex-Dirección Nal.Aviac.Civil e Infraestr. Aeronau.",
    ("3", "31"): "*NO VALE Ex-Dirección General de Aviación Civil",
    ("3", "32"): "*NO VALE Ex-Dirección General Infraestructura Aeronáutica",
    ("3", "33"): "Dirección Nacional de Sanidad de las Fuerzas Armadas",
    ("3", "34"): "Dirección General de los Servicios",
    ("3", "35"): "Servicio de Retiros y Pensiones de las Fuerzas Armadas",
    ("3", "39"): "Dirección Nacional de Meteorología",
    ("3", "40"): "*NO VALE Ex-D.N.Comunicaciones-TRANSIT.Grupo0-A70a88L17296",
    ("3", "41"): "Dirección Nacional Aviación Civil e Infraestructura Aeronáut",
    # Inciso 4 — Ministerio del Interior
    ("4", "1"): "Secretaría del Ministerio del Interior",
    ("4", "2"): "Dirección Nacional de Migración",
    ("4", "3"): "Ex-Intendencia General de Policía NO VIG. desde 1/3/01",
    ("4", "4"): "Jefatura de Policía de Montevideo",
    ("4", "5"): "Jefatura de Policía de Artigas",
    ("4", "6"): "Jefatura de Policía de Canelones",
    ("4", "7"): "Jefatura de Policía de Cerro Largo",
    ("4", "8"): "Jefatura de Policía de Colonia",
    ("4", "9"): "Jefatura de Policía de Durazno",
    ("4", "10"): "Jefatura de Policía de Flores",
    ("4", "11"): "Jefatura de Policía de Florida",
    ("4", "12"): "Jefatura de Policía de Lavalleja",
    ("4", "13"): "Jefatura de Policía de Maldonado",
    ("4", "14"): "Jefatura de Policía de Paysandú",
    ("4", "15"): "Jefatura de Policía de Río Negro",
    ("4", "16"): "Jefatura de Policía de Rivera",
    ("4", "17"): "Jefatura de Policía de Rocha",
    ("4", "18"): "Jefatura de Policía de Salto",
    ("4", "19"): "Jefatura de Policía de San José",
    ("4", "20"): "Jefatura de Policía de Soriano",
    ("4", "21"): "Jefatura de Policía de Tacuarembó",
    ("4", "22"): "Jefatura de Policía de Treinta y Tres",
    ("4", "23"): "Dirección Nacional de Policía Caminera",
    ("4", "24"): "Dirección Nacional de Bomberos",
    ("4", "25"): "Dirección Nacional de Asistencia y Seguridad Social Policial",
    ("4", "26"): "Instituto Nacional de Rehabilitación",
    ("4", "27"): "Dirección Nacional de Información e Inteligencia",
    ("4", "28"): "Dirección Nacional de Policía Científica",
    ("4", "29"): "Escuela Nacional de Policía",
    ("4", "30"): "Dirección Nacional de Sanidad Policial",
    ("4", "31"): "Dirección Nacional de Identificación Civil",
    ("4", "32"): "Dirección Nacional de Prevención Social del Delito",
    ("4", "33"): "Guardia Republicana",
    ("4", "34"): "Dirección Nacional de Asuntos Sociales",
    ("4", "35"): "Dirección Nacional de la Seguridad Rural",
    # Inciso 5 — Ministerio de Economía y Finanzas
    ("5", "1"): "Dir. Gral. Secretaría del Mrio. de Economía y Finanzas",
    ("5", "2"): "Contaduría General de la Nación",
    ("5", "3"): "Auditoría Interna de la Nación",
    ("5", "4"): "Tesorería General de la Nación",
    ("5", "5"): "Dirección General Impositiva",
    ("5", "6"): "Dirección Nacional de Zonas Francas",
    ("5", "7"): "Dirección Nacional de Aduanas",
    ("5", "8"): "Dirección Nacional de Loterías y Quinielas",
    ("5", "9"): "Dirección Nacional de Catastro",
    ("5", "11"): "Secretaría Administrativa del Grupo Mercado Común",
    ("5", "13"): "Dirección General de Casinos",
    ("5", "14"): "Dirección General de Comercio",
    # Inciso 6 — Ministerio de Relaciones Exteriores
    ("6", "1"): "Ministerio de Relaciones Exteriores",
    # Inciso 7 — Ministerio de Ganadería, Agricultura y Pesca
    ("7", "1"): "Dirección General de Secretaría",
    ("7", "2"): "Dir.Nal. de Rec.Acuáticos(Ex-INAPE)",
    ("7", "3"): "Dirección General de Recursos Naturales Renovables",
    ("7", "4"): "Dirección General de Servicios Agrícolas",
    ("7", "5"): "Dirección General de Servicios Ganaderos",
    ("7", "6"): "Dirección General de la Granja",
    ("7", "7"): "Dirección Gral .Desarr.Rural",
    ("7", "8"): "Dirección General Forestal",
    ("7", "9"): "Dirección General de Bioseguridad e Inocuidad Alimentaria",
    # Inciso 8 — Ministerio de Industria, Energía y Minería
    ("8", "1"): "Dirección General de Secretaría",
    ("8", "2"): "Dirección Nacional de Industrias",
    ("8", "4"): "Dirección Nacional de la Propiedad Industrial",
    ("8", "6"): "Dirección Nacional de Aplicaciones de Tecnología Nuclear",
    ("8", "7"): "Dirección Nacional de Minería y Geología",
    ("8", "8"): "Dirección Nacional de Energía",
    ("8", "9"): "Dirección Nacional de Artesanías y Pequeñas y Medianas Empre",
    ("8", "10"): "Dir. Nal. de Telecom.y Serv.comun.audiovisual",
    ("8", "11"): "Autoridad Reguladora Nacional en Radioprotección",
    # Inciso 9 — Ministerio de Turismo
    ("9", "1"): "Dirección General de Secretaría",
    ("9", "2"): "Dirección Nacional de Deporte",
    ("9", "3"): "Dirección Nacional de Turismo",
    # Inciso 10 — Ministerio de Transporte y Obras Públicas
    ("10", "1"): "Despacho de la Secretaría Estado y Oficinas Dependientes",
    ("10", "2"): "Registro Nacional de Empresas de Obras Públicas",
    ("10", "3"): "Dirección Nacional de Vialidad",
    ("10", "4"): "Dirección Nacional de Hidrografía",
    ("10", "5"): "Dirección Nacional de Arquitectura",
    ("10", "6"): "Dirección Nacional de Topografía",
    ("10", "7"): "Dirección Nacional de Transporte",
    ("10", "9"): "Dirección Nacional de Inversiones y Planificación",
    ("10", "10"): "Dirección Nacional de Transporte Ferroviario",
    # Inciso 11 — Ministerio de Educación y Cultura
    ("11", "1"): "Dirección General de Secretaría",
    ("11", "2"): "Dirección de Educación",
    ("11", "3"): "Dirección Nacional de Cultura",
    ("11", "4"): "Museo Histórico Nacional",
    ("11", "5"): "Dirección Centros MEC",
    ("11", "6"): "Museo Nacional de Historia Natural y Antropología",
    ("11", "7"): "Archivo General de la Nación",
    ("11", "8"): "Comisión del Patrimonio Cultural de la Nación",
    ("11", "10"): "Museo Nacional de Artes Visuales",
    ("11", "11"): "Instituto de Investigaciones Biológicas Clemente Estable",
    ("11", "12"): "Dirección Nacional de Innovación, Ciencia y Tecnología",
    ("11", "13"): "Ex-Comisión Nacional de Educación Física NO VIGENTE",
    ("11", "15"): "Dirección General de la Biblioteca Nacional",
    ("11", "16"): "Serv. Oficial Difusión, Radiotelevisión, Espectáculos",
    ("11", "17"): "Fiscalías de Gobierno de Primer y Segundo Turno",
    ("11", "18"): "Dirección General de Registros",
    ("11", "19"): "Fiscalía de Corte, Procuraduría General de la Nación",
    ("11", "20"): "Procuraduría Estado en Contencioso-Administrativo",
    ("11", "21"): "Dirección General del Registro de Estado Civil",
    ("11", "22"): "Junta de Transparencia y Etica Pública-JUTEP",
    ("11", "23"): "Dir.Nal.Biblio.Arch.y Museos Hist.",
    ("11", "24"): "Canal 5 - Servicio de Televisión Nacional",
    ("11", "25"): "Dirección Nacional de Asuntos Constitucionales y Legales",
    # Inciso 12 — Ministerio de Salud Pública
    ("12", "1"): "Dirección General de Secretaría",
    ("12", "2"): "NO VIGENTE Red de Atenc.1er.nivel ASSE",
    ("12", "3"): "NO VIGENTE Unidad de Atención Cardio-Respiratoria",
    ("12", "4"): "NO VIGENTE Centro Hospitalario Pereira Rossell",
    ("12", "5"): "NO VIGENTE Hospital Maciel",
    ("12", "6"): "NO VIGENTE Hospital Pasteur",
    ("12", "7"): "NO VIGENTE Hospital Vilardebó",
    ("12", "8"): "NO VIGENTE Instituto Nacional del Cáncer",
    ("12", "9"): "NO VIGENTE Serv. Nal de Ortopedia y Traumatología",
    ("12", "10"): "NO VIGENTE Inst.Nal.de Reumat.Prof.Dr. Moisés Mizraji",
    ("12", "11"): "NO VIGENTE Ex-Instit.Hanseniano",
    ("12", "12"): "NO VIGENTE Hospital Dr. Gustavo Saint Bois",
    ("12", "13"): "NO VIGENTE Colonia Siquiátrica Dr. Bernardo Etchepare",
    ("12", "14"): "NO VIGENTE Ex-Hosp.Siquiátrico",
    ("12", "15"): "NO VIGENTE Centro Departamental de Artigas",
    ("12", "16"): "NO VIGENTE Centro Departamental de Canelones",
    ("12", "17"): "NO VIGENTE Centro Departamental de Cerro Largo",
    ("12", "18"): "NO VIGENTE Centro Departamental de Sal.Púb.de Colonia",
    ("12", "19"): "NO VIGENTE Centro Departamental de Durazno",
    ("12", "20"): "NO VIGENTE Centro Departamental de Flores",
    ("12", "21"): "NO VIGENTE Centro Departamental de Florida",
    ("12", "22"): "NO VIGENTE Centro Departamental de Lavalleja",
    ("12", "23"): "NO VIGENTE Centro Departamental de Maldonado",
    ("12", "24"): "NO VIGENTE Centro Departamental de Paysandú",
    ("12", "25"): "NO VIGENTE Centro Departamental de Rivera",
    ("12", "26"): "NO VIGENTE Centro Departamental de Río Negro",
    ("12", "27"): "NO VIGENTE Centro Departamental de Rocha",
    ("12", "28"): "NO VIGENTE Centro Departamental de Salto",
    ("12", "29"): "NO VIGENTE Centro Departamental de San José",
    ("12", "30"): "NO VIGENTE Centro Departamental de Soriano",
    ("12", "31"): "NO VIGENTE Centro Departamental de Tacuarembó",
    ("12", "32"): "NO VIGENTE Centro Departamental de Treinta y Tres",
    ("12", "33"): "NO VIGENTE Centro Auxiliar de Aiguá",
    ("12", "34"): "NO VIGENTE Centro Auxiliar de Bella Unión",
    ("12", "35"): "NO VIGENTE Centro Aux.de Cardona y Florencio Sánchez",
    ("12", "36"): "NO VIGENTE Centro Auxiliar de Carmelo",
    ("12", "37"): "NO VIGENTE Centro Auxiliar de Castillos",
    ("12", "38"): "NO VIGENTE Centro Auxiliar de Cerro Chato",
    ("12", "39"): "NO VIGENTE Centro Auxiliar de Dolores",
    ("12", "40"): "NO VIGENTE Centro Auxiliar de Young",
    ("12", "41"): "NO VIGENTE Centro Auxiliar de Guichón",
    ("12", "42"): "NO VIGENTE Centro Auxiliar de José Batlle y Ordoñez",
    ("12", "43"): "NO VIGENTE Centro Auxiliar de Juan Lacaze",
    ("12", "44"): "NO VIGENTE Centro Auxiliar de Lascano",
    ("12", "45"): "NO VIGENTE Centro Auxiliar de Libertad",
    ("12", "46"): "NO VIGENTE Centro Auxiliar de Minas Corrales",
    ("12", "47"): "NO VIGENTE Centro Auxiliar de Nueva Helvecia",
    ("12", "48"): "NO VIGENTE Centro Auxiliar de Nueva Palmira",
    ("12", "49"): "NO VIGENTE Centro Auxiliar de Pan De Azúcar",
    ("12", "50"): "NO VIGENTE Centro Auxiliar de Pando",
    ("12", "51"): "NO VIGENTE Centro Auxiliar de Paso de los Toros",
    ("12", "52"): "NO VIGENTE Centro Auxiliar de Río Branco",
    ("12", "53"): "NO VIGENTE Centro Auxiliar de Rosario",
    ("12", "54"): "NO VIGENTE Centro Auxiliar de San Carlos",
    ("12", "55"): "NO VIGENTE Centro Auxiliar de San Gregorio Polanco",
    ("12", "56"): "NO VIGENTE Centro Auxiliar de San Ramón",
    ("12", "57"): "NO VIGENTE Centro Auxiliar de Santa Lucía",
    ("12", "58"): "NO VIGENTE Centro Auxiliar de Sarandí Grande",
    ("12", "59"): "NO VIGENTE Centro Auxiliar de Sarandí Del Yí",
    ("12", "60"): "NO VIGENTE Centro Auxiliar de Tala",
    ("12", "61"): "NO VIGENTE Centro Auxiliar de Vergara",
    ("12", "62"): "NO VIGENTE Centro Auxiliar de las Piedras",
    ("12", "63"): "NO VIGENTE Hosp-CTro Geriátrico Dr. Luis Piñeiro del Campo",
    ("12", "64"): "NO VIGENTE Laboratorio Químico IndusT.Francisco Dorrego",
    ("12", "65"): "NO VIGENTE Ex-Com.H.Lucha ctr./Hidat.",
    ("12", "66"): "NO VIGENTE Servicio Nacional de Sangre",
    ("12", "67"): "NO VIGENTE Escuela de Sanidad Dr.José Scosería",
    ("12", "68"): "NO VIGENTE Adm.de Servicios de Salud del Estado",
    ("12", "69"): "NO VIGENTE Colonia Dr.Santín Carlos Rossi",
    ("12", "70"): "NO VIGENTE Dirección General de la Salud",
    ("12", "71"): "NO VIGENTE Inst.Nal.Don.yTransp.Cél.Tej.Or",
    ("12", "73"): "NO VIGENTE Centro Auxiliar Chuy",
    ("12", "74"): "NO VIGENTE Centro Auxiliar Rincón de la Bolsa",
    ("12", "75"): "NO VIGENTE Centro Auxiliar Ciudad de la Costa",
    ("12", "76"): "NO VIGENTE Hospital Español",
    ("12", "78"): "NO VIGENTE Ctro Inf.y Ref.Nal de Red Drogas",
    ("12", "102"): "Junta Nacional de Salud",
    ("12", "103"): "Dirección General de la Salud",
    ("12", "104"): "Inst.Nal.Donac.yTrasp.Células,Tej.y Organos",
    ("12", "105"): "Dir.Gral.Sist.Nal.Integrado Salud",
    ("12", "106"): "Dirección General de Coordinación",
    ("12", "108"): "Dirección General de Fiscalización",
    # Inciso 13 — Ministerio de Trabajo y Seguridad Social
    ("13", "1"): "Dirección General de Secretaría",
    ("13", "2"): "Dirección Nacional de Trabajo",
    ("13", "3"): "Dirección Nacional de Empleo",
    ("13", "4"): "Dirección Nacional de Coordinación en el Interior",
    ("13", "5"): "Direc.Nal de Seg.Social",
    ("13", "6"): "Instituto Nacional de Alimentación",
    ("13", "7"): "Inspección General del Trabajo y de la Seguridad Social",
    # Inciso 14 — Ministerio de Vivienda, Ordenamiento Territorial y Medio Ambiente
    ("14", "1"): "Dirección General de Secretaría",
    ("14", "2"): "Dirección Nacional de Vivienda",
    ("14", "3"): "Dirección Nacional de Ordenamiento Territorial",
    ("14", "4"): "Dirección Nacional de Medio Ambiente",
    ("14", "5"): "Dirección Nacional de Aguas (DI.NA.GUA)",
    ("14", "6"): "Dirección Nacional de Integración Social y Urbana",
    # Inciso 15 — Ministerio de Desarrollo Social
    ("15", "1"): "Direc. General de Secretaría.",
    ("15", "2"): "Dirección de Desarrollo Social",
    ("15", "3"): "Instituto Nacional de Alimentación",
    ("15", "5"): "Instituto Nacional de las Mujeres",
    ("15", "6"): "Dirección Nacional de Protección Social",
    ("15", "7"): "Instituto Nacional de la Juventud",
    # Inciso 16 — Poder Judicial
    ("16", "1"): "NO VIGENTE SCJ",
    ("16", "2"): "NO VIG.Trib.Apel.Civ.Pen.,Trab.Juz.Let.Fam.Men.Ad.Penal",
    ("16", "3"): "NO VIGENTE Juzg.Letrad,Primera Inst. Interior Juz.Paz Int.",
    ("16", "4"): "NO VIGENTEInst.TForense,Serv.Asis.Pro Soc,Of.Notif,Dep.Jud",
    ("16", "101"): "Poder Judicial",
    # Inciso 17 — Tribunal de Cuentas
    ("17", "1"): "Tribunal de Cuentas",
    # Inciso 18 — Corte Electoral
    ("18", "1"): "Corte Electoral",
    # Inciso 19 — Tribunal de lo Contencioso Administrativo
    ("19", "1"): "Tribunal de lo Contencioso Administrativo",
    # Inciso 24 — Organismos varios
    ("24", "1"): "UNIDAD NO VIGENTE",
    ("24", "2"): "Presidencia de la República",
    ("24", "3"): "Ministerio de Defensa Nacional",
    ("24", "4"): "Ministerio del Interior",
    ("24", "5"): "Ministerio de Economía y Finanzas",
    ("24", "6"): "Ministerio de Relaciones Exteriores",
    ("24", "7"): "Ministerio de Agricultura y Pesca",
    ("24", "8"): "Ministerio de Industria y Energía",
    ("24", "9"): "Ministerio de Turismo",
    ("24", "10"): "Ministerio de Transporte y Obras Públicas",
    ("24", "11"): "Ministerio de Educación y Cultura",
    ("24", "12"): "Ministerio de Salud Pública",
    ("24", "13"): "Ministerio de Trabajo y Seguridad Social",
    ("24", "14"): "Ministerio de Vivienda, Ordenamiento Territorial y Medio Amb",
    ("24", "15"): "Ministerio de Desarrollo Social",
    ("24", "16"): "Suprema Corte de Justicia",
    ("24", "18"): "Corte Electoral",
    ("24", "19"): "Tribunal de lo Contencioso Administrativo",
    ("24", "24"): "Dir. Gral. de Secretaría (M.E.F.)",
    ("24", "25"): "Administración Nal.de Educación Pública",
    ("24", "26"): "Universidad de la República",
    ("24", "27"): "Instituto Nacional del Menor (INAME)",
    ("24", "29"): "ASSE",
    ("24", "31"): "Universidad Tecnológica del Uruguay",
    ("24", "79"): "Intendencias sin discriminar(OPP- DIPRODE)",
    # Inciso 25 — Consejo de Educación
    ("25", "1"): "Consejo Directivo Central",
    ("25", "2"): "Consejo de Educación Inicial y Primaria",
    ("25", "3"): "Consejo de Educación Secundaria",
    ("25", "4"): "Consejo de Educación Técnico-Profesional",
    ("25", "5"): "Consejo de Formación en Educación",
    # Inciso 26 — Universidad de la República
    ("26", "1"): "Oficinas Centrales y Escuelas Dependientes de Rectorado",
    ("26", "2"): "Facultad de Agronomía",
    ("26", "3"): "Facultad de Arquitectura, Diseño y Urbanismo",
    ("26", "4"): "Facultad de Ciencias Económicas y de Administración",
    ("26", "5"): "Facultad de Derecho",
    ("26", "6"): "Facultad de Ingenieria",
    ("26", "7"): "Facultad de Medicina",
    ("26", "8"): "Instituto de Higiene",
    ("26", "9"): "Facultad de Odontología",
    ("26", "10"): "Facultad de Química",
    ("26", "11"): "Facultad de Veterinaria",
    ("26", "12"): "Facultad de Humanidades y Ciencias de la Educación",
    ("26", "13"): "Regional Norte",
    ("26", "14"): "UNI-BID",
    ("26", "15"): "Hospital de Clínicas",
    ("26", "16"): "Facultad de Artes",
    ("26", "17"): "Centro de Investigaciones Nucleares",
    ("26", "18"): "Escuela Universitaria de Servicio Social",
    ("26", "19"): "Facultad de Psicología",
    ("26", "20"): "Facultad de Bibliotecología",
    ("26", "21"): "Conservatorio Universitario de Música",
    ("26", "22"): "Facultad de Enfermería",
    ("26", "23"): "Facultad de Ciencias Sociales",
    ("26", "24"): "Facultad de Ciencias",
    ("26", "25"): "Facultad de Información y Comunicación",
    ("26", "30"): "Centro Universitario Regional Este",
    ("26", "31"): "Centro Universitario Regional Litoral Norte",
    ("26", "33"): "Centro Universitario Regional Noreste",
    ("26", "50"): "Unidad Central",
    # Inciso 27 — INAU
    ("27", "1"): "Instituto del Niño y Adolescente del Uruguay INAU",
    # Inciso 28 — Banco de Previsión Social
    ("28", "1"): "Banco de Previsión Social",
    ("28", "2"): "Consejo de Prestaciones de Actividad",
    ("28", "3"): "Directorio del Banco de Previsión Social",
    ("28", "5"): "Dir. B.P.S. Cons. Prest. Pas. Ancian. Act. Ases.Trib.",
    ("28", "6"): "Consejo de Administración y Servicios Generales",
    # Inciso 29 — ASSE
    ("29", "2"): "Red de Atención Primaria Area Metropolitana",
    ("29", "3"): "Unidad de Atención Cardio - Respiratoria",
    ("29", "4"): "Centro Hospitalario Pereira Rossell",
    ("29", "5"): "Hospital Maciel",
    ("29", "6"): "Hospital Pasteur",
    ("29", "7"): "Hospital Vilardebó",
    ("29", "8"): "Instituto Nacional del Cáncer",
    ("29", "9"): "Servicio Nacional de Ortopedia y Traumatología",
    ("29", "10"): "Instituto Nal.de Reumatalogía Prof.Dr. Moisés Mizraji",
    ("29", "11"): "NO VIGENTE Ex-Instit.Hanseniano(Dec460/001Ax.ICap.III",
    ("29", "12"): "Hospital Dr. Gustavo Saint Bois",
    ("29", "13"): "Colonia Siquiátrica Dr. Bernardo Etchepare",
    ("29", "14"): "NO VIGENTE Ex-Hosp.Siquiátrico(Dec460/001Ax.ICap.III)",
    ("29", "15"): "Centro Departamental de Artigas",
    ("29", "16"): "Centro Departamental de Canelones",
    ("29", "17"): "Centro Departamental de Cerro Largo",
    ("29", "18"): "Centro Departamental de Salud Pública de Colonia",
    ("29", "19"): "Centro Departamental de Durazno",
    ("29", "20"): "Centro Departamental de Flores",
    ("29", "21"): "Centro Departamental de Florida",
    ("29", "22"): "Centro Departamental de Lavalleja",
    ("29", "23"): "Centro Departamental de Maldonado",
    ("29", "24"): "Centro Departamental de Paysandú",
    ("29", "25"): "Centro Departamental de Rivera",
    ("29", "26"): "Centro Departamental de Río Negro",
    ("29", "27"): "Centro Departamental de Rocha",
    ("29", "28"): "Centro Departamental de Salto",
    ("29", "29"): "Centro Departamental de San José",
    ("29", "30"): "Centro Departamental de Soriano",
    ("29", "31"): "Centro Departamental de Tacuarembó",
    ("29", "32"): "Centro Departamental de Treinta y Tres",
    ("29", "33"): "NO VIGENTE Centro Auxiliar de Aiguá",
    ("29", "34"): "Centro Auxiliar de Bella Unión",
    ("29", "35"): "Centro Aux. de Cardona y Florencio Sánchez",
    ("29", "36"): "Centro Auxiliar de Carmelo",
    ("29", "37"): "Centro Auxiliar de Castillos",
    ("29", "38"): "NO VIGENTE Centro Auxiliar de Cerro Chato",
    ("29", "39"): "Centro Auxiliar de Dolores",
    ("29", "40"): "Centro Auxiliar de Young",
    ("29", "41"): "Red de Atención Primaria de Paysandú",
    ("29", "42"): "Red de Atención Primaria de Lavalleja",
    ("29", "43"): "Centro Auxiliar de Juan Lacaze",
    ("29", "44"): "Red de Atención Primaria de Rocha",
    ("29", "45"): "Red de Atención Primaria de San José",
    ("29", "46"): "Red de Atención Primaria de Rivera",
    ("29", "47"): "Centro Auxiliar de Nueva Helvecia",
    ("29", "48"): "Red de Atención Primaria de Colonia",
    ("29", "49"): "Red de Atención Primaria de Maldonado",
    ("29", "50"): "Centro Auxiliar de Pando",
    ("29", "51"): "Centro Auxiliar de Paso de los Toros",
    ("29", "52"): "Centro Auxiliar de Río Branco",
    ("29", "53"): "Centro Auxiliar de Rosario",
    ("29", "54"): "Hospital de San Carlos",
    ("29", "55"): "Red de Atención Primaria de Tacuarembó",
    ("29", "56"): "NO VIGENTE Centro Auxiliar de San Ramón",
    ("29", "57"): "Red de Atención Primaria de Canelones",
    ("29", "58"): "Red de Atención Primaria de Florida",
    ("29", "59"): "Red de Atención Primaria de Durazno",
    ("29", "60"): "NO VIGENTE Centro Auxiliar de Tala",
    ("29", "61"): "Red de Atención Primaria de Treinta y Tres",
    ("29", "62"): "Centro Auxiliar de las Piedras",
    ("29", "63"): "Hospital -Centro Geriátrico Dr. Luis Piñeiro del Campo",
    ("29", "64"): "Laboratorio Químico Industrial Francisco Dorrego",
    ("29", "65"): "NO VIG.Ex-Com.H.Lucha ctr./Hidat.A133L17556 D67/003",
    ("29", "66"): "Servicio Nacional de Sangre",
    ("29", "67"): "NO VIGENTE Escuela de Sanidad Dr.José Scosería",
    ("29", "68"): "Administración de Servicios de Salud del Estado",
    ("29", "69"): "Colonia Dr.Santín Carlos Rossi",
    ("29", "71"): "NO VIGENTE Inst.Nal.Don.yTransp.Cél.Tej.Or",
    ("29", "73"): "Centro Auxiliar Chuy",
    ("29", "74"): "NO VIGENTE Centro Auxiliar Ciudad del Plata",
    ("29", "75"): "NO VIGENTE Centro Auxiliar Ciudad de la Costa",
    ("29", "76"): "Hospital Español",
    ("29", "77"): "Hospital del Cerro",
    ("29", "78"): "Ctro Inf.y Ref.Nal de Red Drogas",
    ("29", "79"): "Red de Atención Primaria de Artigas",
    ("29", "80"): "Red de Atención Primaria de Cerro Largo",
    ("29", "81"): "Red de Atención Primaria de Flores",
    ("29", "82"): "Red de Atención Primaria de Río Negro",
    ("29", "83"): "Red de Atención Primaria de Soriano",
    ("29", "84"): "Red de Atención Primaria de Salto",
    ("29", "86"): "Direc.Sistema de Atenciòn Integral Personas Privad. Libertad",
    ("29", "87"): "Asistencia Integral",
    ("29", "88"): "Hospital Especializado de Ojos",
    ("29", "89"): "Centro Auxiliar de Sarandí del Yi",
    ("29", "90"): "Centro Auxiliar de Nueva Palmira",
    ("29", "91"): "Centro Auxiliar de Guichón",
    ("29", "101"): "Centro Hospitalario Libertad",
    ("29", "102"): "Centro Departamental de Maldonado",
    ("29", "103"): "Centro de Rehabilitación Médico Ocupacional y Sicosocial",
    ("29", "105"): "Atencion de Urgencia Emergencia Prehospitalaria y Traslado.",
    # Inciso 30 — Organismos varios (presupuesto)
    ("30", "1"): "Poder Legislativo",
    ("30", "2"): "Presidencia de la República",
    ("30", "3"): "Ministerio de Defensa Nacional",
    ("30", "4"): "Ministerio del Interior",
    ("30", "5"): "Ministerio de Economia y Finanzas",
    ("30", "6"): "Ministerio de Relaciones Exteriores",
    ("30", "7"): "Ministerio de Ganadaría, Agricultura y Pesca",
    ("30", "8"): "Ministerio de Industria, Energía",
    ("30", "9"): "Ministerio de Turismo",
    ("30", "10"): "Ministerio de Transporte y Obras Públicas",
    ("30", "11"): "Ministerio de Educación y Cultura",
    ("30", "12"): "Ministerio de Salud Pública",
    ("30", "13"): "Ministerio de Trabajo y Seguridad Social",
    ("30", "14"): "Ministerio de Vivienda, Ordenamiento Territorial y Medio Amb",
    ("30", "16"): "Poder Judicial",
    ("30", "17"): "Tribunal de Cuentas",
    ("30", "18"): "Corte Electoral",
    ("30", "19"): "Tribunal de lo Contencioso Administrativo",
    ("30", "25"): "Administración Nacional de Educación Pública (A.N.E.P.)",
    ("30", "26"): "Universidad de la República",
    ("30", "27"): "Instituto Nacional del Menor (I.NA.ME.)",
    ("30", "30"): "Contaduría General de la Nación (CGN)",
    # Inciso 31 — UTEC
    ("31", "1"): "Universidad Tecnológica del Uruguay",
    ("31", "2"): "Instituto Tecnológico Regional Oeste",
    # Inciso 32 — INUMET
    ("32", "1"): "Instituto Uruguayo de Meteorologia INUMET",
    # Inciso 33 — Fiscalía General
    ("33", "1"): "Fiscalia General de la Nación",
    # Inciso 34 — JUTEP
    ("34", "1"): "Junta de Transparencia y Etica Publica",
    # Inciso 35 — INIA
    ("35", "1"): "Instituto Nacional de Inclusion Social Adolescente",
    # Inciso 36 — MGAP (reorganizado)
    ("36", "1"): "Dirección General de Secretaría",
    ("36", "2"): "Dirección Nacional de Calidad y Evaluación Ambiental",
    ("36", "3"): "Dirección Nacional de Aguas (DINAGUA)",
    ("36", "4"): "Dirección Nacional de Biodiversidad y Servicios Ecosistémico",
    ("36", "5"): "Dirección Nacional de Cambio Climático",
    # Inciso 40 — Entes autónomos y servicios descentralizados
    ("40", "1"): "Fondo Nacional de Solidaridad",
    ("40", "2"): "Fondo Nacional de Recursos",
    ("40", "3"): "Comis.Hon.Lucha Contra Cáncer",
    ("40", "4"): "Comis.Hon.para la Lucha Antituberc.y Enferm. Prevalentes",
    ("40", "5"): "Comis.Hon.de la Salud Cardiovascular",
    ("40", "6"): "MEVIR",
    ("40", "7"): "Fondo Seguro de Salud (OSE)",
    ("40", "8"): "COCAP",
    ("40", "9"): "INAC",
    ("40", "10"): "INAVI",
    ("40", "11"): "NO VIGENTE-Com.N.H.LuchaContraHidatidósis",
    ("40", "12"): "Fondo Nal. de Música",
    ("40", "13"): "URUGUAY XXI",
    ("40", "14"): "Plan Agropecuario",
    ("40", "15"): "Impresiones y Publics.Oficiales (IMPO)",
    ("40", "16"): "Inst.Nal. de Inves.Agropecuaria (INIA)",
    ("40", "17"): "Instituto Nal. de Semillas",
    ("40", "18"): "Adm.del Mercado Eléctrico (ADME)",
    ("40", "19"): "LATU - Laboratorio Tecnológico del Uruguay",
    ("40", "20"): "Corporación Nacional para el Desarrollo (CND)",
    ("40", "21"): "NO VIGENTE-Fdo.Nal del Teatro (FNT)",
    ("40", "22"): "Fondo de Cesantía y Retiro(Construcción)",
    ("40", "23"): "Instituto Nacional de la Leche",
    ("40", "24"): "Fdo.Fin.y Des.Susten.de la Act.Lechera",
    ("40", "25"): "Instituto Nacional de Calidad",
    ("40", "26"): "ANII - Agencia Nal. de Investigación e Innovación",
    ("40", "27"): "Centro Uruguayo de Imagenología Molecular",
    ("40", "28"): "Parque Científico y Tecnológico de Pando",
    ("40", "29"): "Corporación de Protección del Ahorro Bancario",
    ("40", "30"): "Instituto Nacional del Cooperativismo",
    ("40", "31"): "Inst.Nal.de Evaluación Educativa",
    ("40", "32"): "Colegio Médico del Uruguay",
    ("40", "33"): "Agencia Nacional de Desarrollo (ANDE)",
    ("40", "34"): "Ctro Ceibal para el Apoyo a la Educ. de Niñez y Adolesc.",
    ("40", "35"): "Inst.Nal. de Empleo y Formación Profesional",
    ("40", "39"): "Instituto de Regulación y Control del Cannabis (IRCCA)",
    # Inciso 47 — Caja de Jubilaciones
    ("47", "1"): "Caja de Jub. y Pens. de Profesionales Universitarios",
    # Inciso 50 — Banco Central
    ("50", "1"): "Banco Central del Uruguay",
    # Inciso 51 — Banco República
    ("51", "1"): "Banco de la República del Uruguay",
    # Inciso 52 — Banco Hipotecario
    ("52", "1"): "Banco Hipotecario del Uruguay",
    # Inciso 53 — Banco de Seguros del Estado
    ("53", "1"): "Banco de Seguros del Estado",
    # Inciso 59 — Organismos internacionales
    ("59", "1"): "Banco Mundial (BM)",
    ("59", "2"): "Fondo Monetario Internacional (FMI)",
    ("59", "3"): "Banco Interamericano de Desarrollo (BID)",
    ("59", "4"): "Banco Internacional de Reconstrucción y Fomento (BIRF)",
    ("59", "5"): "Corporación Financiera Internacional (CFI)",
    ("59", "6"): "Corporación Interamericana de Inversiones(CII)",
    ("59", "7"): "Organismo Multilateral de Garantía de Inversiones (OMGI)",
    # Inciso 60 — ANCAP
    ("60", "1"): "Administración Nacional de Combustible, Alcohol y Portland",
    # Inciso 61 — UTE
    ("61", "1"): "Administración Nacional de Usinas y Trasmisiones Eléctricas",
    # Inciso 62 — AFE
    ("62", "1"): "Administración de los Ferrocarriles del Estado",
    # Inciso 63 — PLUNA
    ("63", "1"): "Primeras Líneas Uruguayas de Navegación Aérea",
    # Inciso 64 — ANP
    ("64", "1"): "Administración Nacional de Puertos",
    # Inciso 65 — ANTEL
    ("65", "1"): "Administración Nacional de Telecomunicaciones",
    # Inciso 66 — OSE
    ("66", "1"): "Administración de las Obras Sanitarias del Estado",
    # Inciso 67 — Correo Uruguayo
    ("67", "1"): "Adminstración Nacional de Correos",
    # Inciso 68 — ANV
    ("68", "1"): "Agencia Nacional de Vivienda",
    # Inciso 69 — URSEA
    ("69", "1"): "Unidad Reguladora de Servicios de Energía y Agua",
    # Inciso 70 — Instituto Nacional de Colonización
    ("70", "1"): "Instituto Nacional de Colonización",
    # Inciso 71 — URSEC
    ("71", "1"): "Unidad Reguladora de Servicios de Comunicaciones (URSEC)",
    # Inciso 72 — INAC
    ("72", "1"): "Instituto Nacional de Carnes",
    # Inciso 79 — Intendencias (sin discriminar)
    ("79", "1"): "Intendencias Municipales (sin discriminar)",
    # Inciso 80 — Intendencia de Artigas
    ("80", "1"): "Intendencia de Artigas",
    ("80", "2"): "Junta Departamental de Artigas",
    # Inciso 81 — Intendencia de Canelones
    ("81", "1"): "Intendencia de Canelones",
    ("81", "2"): "Junta Departamental de Canelones",
    # Inciso 82 — Intendencia de Cerro Largo
    ("82", "1"): "Intendencia de Cerro Largo",
    ("82", "2"): "Junta Departamental de Cerro Largo",
    # Inciso 83 — Intendencia de Colonia
    ("83", "1"): "Intendencia de Colonia",
    ("83", "2"): "Junta Departamental de Colonia",
    # Inciso 84 — Intendencia de Durazno
    ("84", "1"): "Intendencia de Durazno",
    ("84", "2"): "Junta Departamental de Durazno",
    # Inciso 85 — Intendencia de Flores
    ("85", "1"): "Intendencia de Flores",
    ("85", "2"): "Junta Departamental de Flores",
    # Inciso 86 — Intendencia de Florida
    ("86", "1"): "Intendencia de Florida",
    ("86", "2"): "Junta Departamental de Florida",
    # Inciso 87 — Intendencia de Lavalleja
    ("87", "1"): "Intendencia de Lavalleja",
    ("87", "2"): "Junta Departamental de Lavalleja",
    # Inciso 88 — Intendencia de Maldonado
    ("88", "1"): "Intendencia de Maldonado",
    ("88", "2"): "Junta Departamental de Maldonado",
    # Inciso 89 — Intendencia de Paysandú
    ("89", "1"): "Intendencia de Paysandú",
    ("89", "2"): "Junta Departamental de Paysandú",
    # Inciso 90 — Intendencia de Río Negro
    ("90", "1"): "Intendencia de Río Negro",
    ("90", "2"): "Junta Departamental de Río Negro",
    # Inciso 91 — Intendencia de Rivera
    ("91", "1"): "Intendencia de Rivera",
    ("91", "2"): "Junta Departamental de Rivera",
    # Inciso 92 — Intendencia de Rocha
    ("92", "1"): "Intendencia de Rocha",
    ("92", "2"): "Junta Departamental de Rocha",
    # Inciso 93 — Intendencia de Salto
    ("93", "1"): "Intendencia de Salto",
    ("93", "2"): "Junta Departamental de Salto",
    # Inciso 94 — Intendencia de San José
    ("94", "1"): "Intendencia de San José",
    ("94", "2"): "Junta Departamental de San José",
    # Inciso 95 — Intendencia de Soriano
    ("95", "1"): "Intendencia de Soriano",
    ("95", "2"): "Junta Departamental de Soriano",
    # Inciso 96 — Intendencia de Tacuarembó
    ("96", "1"): "Intendencia de Tacuarembó",
    ("96", "2"): "Junta Departamental de Tacuarembó",
    # Inciso 97 — Intendencia de Treinta y Tres
    ("97", "1"): "Intendencia de Treinta y Tres",
    ("97", "2"): "Junta Departamental de Treinta y Tres",
    # Inciso 98 — Intendencia de Montevideo
    ("98", "1"): "Intendencia de Montevideo",
    ("98", "2"): "Junta Departamental de Montevideo",
    # Inciso 99 — Congreso de Intendentes
    ("99", "1"): "Congreso de Intendentes",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_organism(id_inciso: int | None, id_ue: int | None) -> str:
    """Return the organism name for a ``(id_inciso, id_ue)`` pair.

    Parameters
    ----------
    id_inciso, id_ue:
        Procurement-system identifiers extracted from the XML
        ``<compra>`` attributes. ``None`` is treated like any other
        unmapped key — the function returns the
        ``"Desconocido ({id_inciso}-{id_ue})"`` fallback and logs a
        warning.

    Returns
    -------
    str
        The organism name when the pair exists in
        :data:`ORGANISM_MAP`, or
        ``"Desconocido ({id_inciso}-{id_ue})"`` otherwise.

    Notes
    -----
    The function is a pure read — no I/O, no cache invalidation, no
    side effects beyond logging. The contract is intentionally
    forgiving: an unmapped combination is a *log event* plus a
    placeholder string, never an exception. Because ``Compra.organismo``
    is nullable, an unmapped pair uses the
    ``"Desconocido ({id_inciso}-{id_ue})"`` fallback, so the ``compra`` row
    is never stored with a ``NULL`` organismo. The pipeline must always
    produce a record.
    """

    key = (
        str(id_inciso) if id_inciso is not None else None,
        str(id_ue) if id_ue is not None else None,
    )
    name = ORGANISM_MAP.get(key)  # type: ignore[arg-type]
    if name is not None:
        return name

    fallback = f"{UNKNOWN_ORGANISM_PREFIX} ({id_inciso}-{id_ue})"
    logger.warning(
        "Unmapped (id_inciso, id_ue) pair: (%r, %r); using fallback %r",
        id_inciso,
        id_ue,
        fallback,
    )
    return fallback


__all__ = ["ORGANISM_MAP", "UNKNOWN_ORGANISM_PREFIX", "resolve_organism"]
