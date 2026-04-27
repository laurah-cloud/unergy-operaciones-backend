"""
Endpoints de compatibilidad para fallas-unergy.
Traduce entre el formato de la app vanilla-JS y nuestra base de datos PostgreSQL.

Convención de estados:
  fallas-unergy  ←→  FallaCatEstado.codigo
  activa         ←→  abierta
  revision       ←→  en_gestion
  programada     ←→  en_espera
  terminada      ←→  cerrada / sin_solucion (ambos → terminada al salir)
"""
import json
import random
import string
from datetime import datetime, date, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, selectinload
from app.core.database import get_db
from app.api.v1.auth import get_current_user
from app.models import (
    Falla, FallaSeguimiento, FallaCatEstado, FallaCatPrioridad,
    FallaCatTipo, FallaCatCategoria, FallaCatResolucion, GeneracionDiaria,
    MonitoreoVerificacion,
)
from app.models.usuarios import Usuario
from app.models.proyectos import Proyecto
from app.utils.proyecto_matching import find_proyecto_by_name

router = APIRouter(prefix="/monitoreo", tags=["Monitoreo"])

# ── mapeos de estado ──────────────────────────────────────────────────────────
_CODIGO_A_ST = {
    "abierta": "activa",
    "en_gestion": "revision",
    "en_espera": "programada",
    "cerrada": "terminada",
    "sin_solucion": "terminada",
}
_ST_A_CODIGO = {
    "activa": "abierta",
    "revision": "en_gestion",
    "programada": "en_espera",
    "terminada": "cerrada",
}

_FALLA_EAGER = [
    selectinload(Falla.proyecto),
    selectinload(Falla.tipo).selectinload(FallaCatTipo.categoria),
    selectinload(Falla.estado),
    selectinload(Falla.prioridad),
    selectinload(Falla.resolucion),
    selectinload(Falla.registrado_por),
    selectinload(Falla.asignado_a),
    selectinload(Falla.seguimientos).options(
        selectinload(FallaSeguimiento.usuario),
        selectinload(FallaSeguimiento.estado_nuevo),
    ),
]


def _falla_to_fault(f: Falla) -> dict:
    """Serializa una Falla de PostgreSQL al formato interno de fallas-unergy."""
    st = _CODIGO_A_ST.get(f.estado.codigo if f.estado else "abierta", "activa")

    # construir seguimiento acumulado (mismo formato que Google Sheets)
    seguimiento_txt = ""
    if f.seguimientos:
        lineas = []
        for seg in sorted(f.seguimientos, key=lambda s: s.created_at):
            ts = seg.created_at.strftime("%d/%m/%Y %H:%M") if seg.created_at else ""
            quien = seg.usuario.nombre if seg.usuario else "—"
            nota = seg.nota or ""
            lineas.append(f"{ts} - {quien}: {nota}")
        seguimiento_txt = "\n".join(lineas)

    fotos_lista: list[str] = []
    if f.fotos_urls:
        try:
            fotos_lista = json.loads(f.fotos_urls)
        except (json.JSONDecodeError, TypeError):
            fotos_lista = []

    return {
        "id": f.codigo_interno,
        "proj": f.proyecto.nombre_comercial if f.proyecto else "",
        "code": f.tipo.codigo if f.tipo else "",
        "faultLabel": f.tipo.etiqueta if f.tipo else "",
        "st": st,
        "date": f.fecha_identificacion.isoformat() if f.fecha_identificacion else "",
        "time": f.hora_identificacion.strftime("%H:%M") if f.hora_identificacion else "",
        "occ": f.fecha_ocurrencia.strftime("%d/%m/%Y %H:%M") if f.fecha_ocurrencia else "",
        "res": f.resolucion.etiqueta if f.resolucion else "",
        "desc": f.descripcion or "",
        "flw": seguimiento_txt,
        "driveUrl": fotos_lista[0] if fotos_lista else "",
        "driveUrls": fotos_lista,
        "endDT": f.fecha_resolucion.strftime("%d/%m/%Y %H:%M") if f.fecha_resolucion else "",
        "centinela": f.centinela or "",
        "prio": f.prioridad.codigo if f.prioridad else "media",
        "notify": bool(f.notificacion),
        "photos": [],
        # campos extra útiles para el frontend
        "_db_id": f.id,
        "_dias_abierta": f.dias_abierta,
        "_categoria_id": str(f.tipo.categoria.codigo) if f.tipo and f.tipo.categoria else "",
        "_categoria_lbl": f.tipo.categoria.etiqueta if f.tipo and f.tipo.categoria else "",
    }


# ── GET /monitoreo/fallas ─────────────────────────────────────────────────────
@router.get("/fallas")
def get_fallas_monitoreo(
    proyecto_id: int | None = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """Devuelve las fallas en el formato esperado por fallas-unergy."""
    q = db.query(Falla).options(*_FALLA_EAGER)

    # si el usuario es de rol cliente, filtrar solo sus proyectos
    if current_user.rol.value == "solo_lectura":
        # TODO: filtrar por proyectos del cliente cuando se implemente
        pass

    if proyecto_id:
        q = q.filter(Falla.proyecto_id == proyecto_id)

    fallas = q.order_by(Falla.created_at.desc()).all()
    results = []
    for f in fallas:
        try:
            results.append(_falla_to_fault(f))
        except Exception:
            pass
    return {"ok": True, "faults": results}
@router.get("/debug")
def debug_monitoreo(db: Session = Depends(get_db), _=Depends(get_current_user)):
    import traceback
    try:
        count = db.query(Falla).count()
        falla = db.query(Falla).options(*_FALLA_EAGER).first()
        result = _falla_to_fault(falla) if falla else None
        return {"ok": True, "count": count, "primera_falla": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "trace": traceback.format_exc()}

# ── POST /monitoreo/fallas/save ───────────────────────────────────────────────
@router.post("/fallas/save")
def save_falla_monitoreo(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """Crea o actualiza una falla desde fallas-unergy."""
    fault_id: str = payload.get("id", "").strip()
    is_new = not fault_id

    # ── resolver proyecto ─────────────────────────────────────────────────
    proyecto: Proyecto | None = None
    nombre_proyecto = payload.get("project", "").strip()
    if nombre_proyecto:
        proyecto = find_proyecto_by_name(db, nombre_proyecto)
    if not proyecto:
        raise HTTPException(400, f"No se encontró proyecto para '{nombre_proyecto}'")

    # ── resolver estado ───────────────────────────────────────────────────
    st_code = _ST_A_CODIGO.get(payload.get("status", "activa"), "abierta")
    estado = db.query(FallaCatEstado).filter(FallaCatEstado.codigo == st_code).first()
    if not estado:
        raise HTTPException(400, f"Estado no reconocido: {st_code}")

    # ── resolver tipo de falla ────────────────────────────────────────────
    fault_code = payload.get("faultCode", "").strip()
    tipo = db.query(FallaCatTipo).filter(FallaCatTipo.codigo == fault_code).first()
    if not tipo:
        # fallback: primer tipo disponible de la categoría (por ID numérico)
        cat_id_str = str(payload.get("categoryId", "1"))
        cat = db.query(FallaCatCategoria).filter(FallaCatCategoria.codigo == cat_id_str).first()
        tipo = (
            db.query(FallaCatTipo)
            .filter(FallaCatTipo.categoria_id == cat.id, FallaCatTipo.activa == True)
            .first()
            if cat else None
        )
        if not tipo:
            raise HTTPException(400, f"Tipo de falla no reconocido: {fault_code}")

    # ── resolver prioridad ────────────────────────────────────────────────
    prio_codigo = (payload.get("prioridad") or "media").lower()
    prioridad = db.query(FallaCatPrioridad).filter(FallaCatPrioridad.codigo == prio_codigo).first()
    if not prioridad:
        prioridad = db.query(FallaCatPrioridad).order_by(FallaCatPrioridad.nivel).first()

    # ── resolver resolución ───────────────────────────────────────────────
    resolucion = None
    res_texto = (payload.get("resType") or "").strip()
    if res_texto:
        resolucion = db.query(FallaCatResolucion).filter(
            FallaCatResolucion.etiqueta.ilike(f"%{res_texto}%")
        ).first()

    # ── parsear fechas ─────────────────────────────────────────────────────
    def _parse_date(s: str) -> date | None:
        if not s:
            return None
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(s.split(" ")[0], fmt).date()
            except ValueError:
                continue
        return None

    def _parse_datetime(s: str) -> datetime | None:
        if not s:
            return None
        for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    fecha_id = _parse_date(payload.get("identDate", "")) or date.today()
    fecha_ocurrencia = _parse_datetime(payload.get("occTime", ""))
    fecha_resolucion = _parse_datetime(payload.get("endTime", ""))

    # ── fotos: solo almacenar URLs, no subir nada ─────────────────────────
    fotos_urls_payload = payload.get("driveUrls") or []
    drive_url = payload.get("driveUrl", "").strip()
    if drive_url and drive_url not in fotos_urls_payload:
        fotos_urls_payload = [drive_url] + fotos_urls_payload
    fotos_json = json.dumps(fotos_urls_payload) if fotos_urls_payload else None

    centinela = (payload.get("centinela") or "").strip() or current_user.nombre
    followup_nuevo = (payload.get("followUp") or "").strip()

    if is_new:
        # ── crear nueva falla ─────────────────────────────────────────────
        from datetime import timezone as tz
        from sqlalchemy import func as sqlfunc
        year = datetime.now(tz.utc).year
        count = db.query(sqlfunc.count(Falla.id)).scalar() or 0
        codigo = f"FAL-{year}-{count + 1:05d}"

        falla = Falla(
            codigo_interno=codigo,
            proyecto_id=proyecto.id,
            tipo_id=tipo.id,
            estado_id=estado.id,
            prioridad_id=prioridad.id,
            resolucion_id=resolucion.id if resolucion else None,
            registrado_por_id=current_user.id,
            descripcion=payload.get("desc") or payload.get("faultLabel") or "Sin descripción",
            fecha_identificacion=fecha_id,
            fecha_ocurrencia=fecha_ocurrencia,
            fecha_resolucion=fecha_resolucion,
            fotos_urls=fotos_json,
            centinela=centinela,
            notificacion=bool(payload.get("notify", False)),
        )
        db.add(falla)
        db.flush()  # obtener ID

        if followup_nuevo:
            seg = FallaSeguimiento(
                falla_id=falla.id,
                usuario_id=current_user.id,
                nota=followup_nuevo,
            )
            db.add(seg)
    else:
        # ── actualizar falla existente ────────────────────────────────────
        falla = (
            db.query(Falla)
            .options(*_FALLA_EAGER)
            .filter(Falla.codigo_interno == fault_id)
            .first()
        )
        if not falla:
            raise HTTPException(404, f"Falla {fault_id} no encontrada")

        falla.proyecto_id = proyecto.id
        falla.tipo_id = tipo.id
        falla.estado_id = estado.id
        falla.prioridad_id = prioridad.id
        falla.resolucion_id = resolucion.id if resolucion else None
        falla.descripcion = payload.get("desc") or payload.get("faultLabel") or falla.descripcion
        falla.fecha_identificacion = fecha_id
        falla.fecha_ocurrencia = fecha_ocurrencia
        falla.fecha_resolucion = fecha_resolucion
        falla.fotos_urls = fotos_json
        falla.centinela = centinela
        falla.notificacion = bool(payload.get("notify", False))

        # agregar seguimiento solo si el texto cambió respecto al último
        if followup_nuevo:
            segs_existentes = [
                s for s in sorted(falla.seguimientos, key=lambda s: s.created_at)
            ]
            ultimo_flw = segs_existentes[-1].nota if segs_existentes else ""
            if followup_nuevo != ultimo_flw:
                seg = FallaSeguimiento(
                    falla_id=falla.id,
                    usuario_id=current_user.id,
                    nota=followup_nuevo,
                    estado_nuevo_id=estado.id,
                )
                db.add(seg)

    db.commit()

    # recargar con eager loading para respuesta
    falla_out = (
        db.query(Falla)
        .options(*_FALLA_EAGER)
        .filter(Falla.codigo_interno == (falla.codigo_interno if is_new else fault_id))
        .first()
    )
    return {"ok": True, "fault": _falla_to_fault(falla_out)}


# ── POST /monitoreo/fallas/delete ─────────────────────────────────────────────
@router.post("/fallas/delete")
def delete_falla_monitoreo(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    fault_id = (payload.get("id") or "").strip()
    if not fault_id:
        raise HTTPException(400, "Se requiere id")
    falla = db.query(Falla).filter(Falla.codigo_interno == fault_id).first()
    if not falla:
        raise HTTPException(404, f"Falla {fault_id} no encontrada")
    db.delete(falla)
    db.commit()
    return {"ok": True}


# ── GET /monitoreo/catalogo ───────────────────────────────────────────────────
@router.get("/catalogo")
def get_catalogo(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Devuelve el catálogo de fallas en el formato DEFAULT_CATS de fallas-unergy."""
    categorias = (
        db.query(FallaCatCategoria)
        .options(selectinload(FallaCatCategoria.tipos))
        .filter(FallaCatCategoria.activa == True)
        .order_by(FallaCatCategoria.orden)
        .all()
    )
    result = []
    for cat in categorias:
        tipos_activos = [t for t in cat.tipos if t.activa]
        result.append({
            "id": cat.codigo,
            "lbl": cat.etiqueta,
            "ico": cat.icono or "📋",
            "col": cat.color_hex or "#915BD8",
            "faults": [
                {"code": t.codigo, "label": t.etiqueta, "desc": t.descripcion or ""}
                for t in sorted(tipos_activos, key=lambda t: t.codigo)
            ],
        })
    return result


# ── GET /monitoreo/proyectos ──────────────────────────────────────────────────
@router.get("/proyectos")
def get_proyectos_monitoreo(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Devuelve la lista de proyectos para poblar los selectores de fallas-unergy."""
    proyectos = (
        db.query(Proyecto)
        .filter(Proyecto.estado == "en_operacion")
        .order_by(Proyecto.nombre_comercial)
        .all()
    )
    return {
        "proyectos": [p.nombre_comercial for p in proyectos],
        "proyectos_detalle": [
            {"id": p.id, "nombre": p.nombre_comercial, "alias": p.alias_monitoreo or ""}
            for p in proyectos
        ],
    }


# ── GET /monitoreo/generacion ─────────────────────────────────────────────────
@router.get("/generacion")
def get_generacion_monitoreo(
    proyecto_nombre: str | None = Query(None),
    proyecto_id: int | None = Query(None),
    fecha_inicio: date | None = Query(None),
    fecha_fin: date | None = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Devuelve datos de generación para el panel de generación de fallas-unergy."""
    pid = proyecto_id
    if not pid and proyecto_nombre:
        proy = find_proyecto_by_name(db, proyecto_nombre)
        if proy:
            pid = proy.id

    q = db.query(GeneracionDiaria)
    if pid:
        q = q.filter(GeneracionDiaria.proyecto_id == pid)
    if fecha_inicio:
        q = q.filter(GeneracionDiaria.fecha >= fecha_inicio)
    if fecha_fin:
        q = q.filter(GeneracionDiaria.fecha <= fecha_fin)

    rows = q.order_by(GeneracionDiaria.fecha).all()
    return {
        "ok": True,
        "datos": [
            {
                "fecha": r.fecha.isoformat(),
                "kwh_real": float(r.kwh_real) if r.kwh_real is not None else None,
                "kwh_p90": float(r.kwh_p90) if r.kwh_p90 is not None else None,
                "kwh_autoconsumo": float(r.kwh_autoconsumo) if r.kwh_autoconsumo is not None else None,
            }
            for r in rows
        ],
    }


# ── POST /monitoreo/auth/verify-email ────────────────────────────────────────
@router.post("/auth/verify-email")
def verify_email_monitoreo(payload: dict, db: Session = Depends(get_db)):
    """Valida que un email @unergy tenga usuario en la plataforma.
    Usado por fallas-unergy cuando no viene token de la plataforma."""
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email requerido")

    user = db.query(Usuario).filter(Usuario.email == email, Usuario.activo == True).first()
    if not user:
        return {"ok": False, "msg": "Correo no registrado en la plataforma"}

    return {
        "ok": True,
        "nombre": user.nombre,
        "email": user.email,
        "rol": user.rol.value,
    }


# ── POST /monitoreo/auth/send-code ────────────────────────────────────────────
@router.post("/auth/send-code")
def send_code(payload: dict, db: Session = Depends(get_db)):
    """Genera y almacena un código de 6 dígitos para acceso de clientes.
    El envío del email debe configurarse con SMTP/SendGrid en producción."""
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Email requerido")

    codigo = "".join(random.choices(string.digits, k=6))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    # invalidar códigos anteriores del mismo email
    db.query(MonitoreoVerificacion).filter(
        MonitoreoVerificacion.email == email,
        MonitoreoVerificacion.usado == False,
    ).delete()

    verificacion = MonitoreoVerificacion(
        email=email,
        codigo=codigo,
        expires_at=expires_at,
    )
    db.add(verificacion)
    db.commit()

    # TODO: integrar envío real de email (SendGrid / SES / SMTP)
    # Por ahora, el código se puede ver en los logs de desarrollo
    print(f"[MONITOREO] Código para {email}: {codigo}")

    return {"ok": True}


# ── POST /monitoreo/auth/verify-code ─────────────────────────────────────────
@router.post("/monitoreo/auth/verify-code")
def verify_code(payload: dict, db: Session = Depends(get_db)):
    """Verifica el código de 6 dígitos y devuelve los proyectos del cliente."""
    email = (payload.get("email") or "").strip().lower()
    codigo = (payload.get("code") or "").strip()

    if not email or not codigo:
        raise HTTPException(400, "Email y código requeridos")

    now = datetime.now(timezone.utc)
    verif = (
        db.query(MonitoreoVerificacion)
        .filter(
            MonitoreoVerificacion.email == email,
            MonitoreoVerificacion.codigo == codigo,
            MonitoreoVerificacion.usado == False,
            MonitoreoVerificacion.expires_at > now,
        )
        .first()
    )

    if not verif:
        return {"ok": False}

    verif.usado = True
    db.commit()

    # buscar proyectos asociados al email del cliente
    proyectos = (
        db.query(Proyecto)
        .join(Proyecto.cliente)
        .filter(
            Proyecto.estado == "en_operacion",
        )
        .all()
    )
    # filtrar proyectos donde el cliente tiene correo coincidente
    proyectos_cliente = [
        p.nombre_comercial for p in proyectos
        if p.cliente and p.cliente.correo and p.cliente.correo.lower() == email
    ]

    return {"ok": True, "projects": proyectos_cliente, "email": email}
