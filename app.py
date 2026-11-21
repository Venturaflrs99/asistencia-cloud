import hashlib
import hmac
import io
import logging
import re
import time
import uuid
import warnings
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3
import pandas as pd
import streamlit as st
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps, UnidentifiedImageError


st.set_page_config(
    page_title="Asistencia Cloud",
    page_icon=None,
    layout="wide",
)

LOGGER = logging.getLogger("asistencia")
TZ = ZoneInfo("America/Tegucigalpa")
COOLDOWN_SECONDS = 120
MAX_FILE_BYTES = 5 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000
SERIALIZER = TypeSerializer()


class BusinessError(Exception):
    pass


@st.cache_resource
def aws():
    session = boto3.Session(
        aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=st.secrets.get("AWS_SESSION_TOKEN"),
        region_name=st.secrets["AWS_REGION"],
    )
    config = Config(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=20,
    )
    resource = session.resource("dynamodb", config=config)
    return {
        "rek": session.client("rekognition", config=config),
        "s3": session.client("s3", config=config),
        "ddb": session.client("dynamodb", config=config),
        "employees": resource.Table(st.secrets["EMPLEADOS_TABLE"]),
        "attendance": resource.Table(st.secrets["ASISTENCIAS_TABLE"]),
    }


def serialize(item):
    return {
        key: SERIALIZER.serialize(value)
        for key, value in item.items()
    }


def now_local():
    return datetime.now(TZ)


def iso_local(value):
    return value.astimezone(TZ).isoformat(timespec="microseconds")


def normalize_photo(upload):
    if upload is None:
        raise BusinessError("Capture o seleccione una fotografía.")

    raw = upload.getvalue()
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise BusinessError("La imagen debe pesar entre 1 byte y 5 MB.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format not in {"JPEG", "PNG"}:
                    raise BusinessError("Solo se permiten imágenes JPEG o PNG.")
                image = ImageOps.exif_transpose(original).convert("RGB")
                image.thumbnail((1600, 1600))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=90)
                result = output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise BusinessError("La imagen no es válida o es demasiado grande.") from exc

    if len(result) > MAX_FILE_BYTES:
        raise BusinessError("La imagen procesada supera 5 MB.")
    return result


def require_one_face(photo):
    result = aws()["rek"].detect_faces(
        Image={"Bytes": photo},
        Attributes=["DEFAULT"],
    )
    count = len(result.get("FaceDetails", []))
    if count != 1:
        raise BusinessError(
            f"La fotografía debe contener exactamente 1 rostro; "
            f"se detectaron {count}."
        )


def get_employee(employee_id):
    return aws()["employees"].get_item(
        Key={"empleado_id": employee_id},
        ConsistentRead=True,
    ).get("Item")


def scan_all(table):
    items = []
    arguments = {"ConsistentRead": True}
    while True:
        response = table.scan(**arguments)
        items.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return items
        arguments["ExclusiveStartKey"] = last


def latest_attendance(employee_id):
    response = aws()["attendance"].query(
        KeyConditionExpression=Key("empleado_id").eq(employee_id),
        ScanIndexForward=False,
        Limit=1,
        ConsistentRead=True,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def index_photo(employee_id, photo):
    response = aws()["rek"].index_faces(
        CollectionId=st.secrets["REKOGNITION_COLLECTION"],
        Image={"Bytes": photo},
        ExternalImageId=employee_id,
        MaxFaces=1,
        QualityFilter="AUTO",
        DetectionAttributes=["DEFAULT"],
    )
    records = response.get("FaceRecords", [])
    if len(records) != 1:
        raise BusinessError(
            "No se pudo indexar el rostro. Use una foto frontal, "
            "nítida y con buena iluminación."
        )
    return records[0]["Face"]["FaceId"]


def delete_face(face_id):
    if not face_id:
        return
    response = aws()["rek"].delete_faces(
        CollectionId=st.secrets["REKOGNITION_COLLECTION"],
        FaceIds=[face_id],
    )
    if response.get("UnsuccessfulFaceDeletions"):
        raise BusinessError("AWS no pudo eliminar el vector facial.")


def cleanup_registration(face_id, s3_key):
    pending = []
    if face_id:
        try:
            delete_face(face_id)
        except (ClientError, BotoCoreError, BusinessError):
            pending.append("vector facial")
    if s3_key:
        try:
            aws()["s3"].delete_object(
                Bucket=st.secrets["S3_BUCKET"],
                Key=s3_key,
            )
        except (ClientError, BotoCoreError):
            pending.append("fotografía S3")
    return pending


def register_employee(employee_id, name, position, department, photo):
    employee_id = employee_id.strip()
    name, position, department = [
        value.strip() for value in (name, position, department)
    ]

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", employee_id):
        raise BusinessError(
            "El ID debe tener de 1 a 64 letras, números, guiones "
            "o guiones bajos."
        )
    if not all((name, position, department)):
        raise BusinessError("Complete todos los datos.")
    if any(len(value) > 150 for value in (name, position, department)):
        raise BusinessError("Cada campo admite como máximo 150 caracteres.")
    if get_employee(employee_id):
        raise BusinessError("Ya existe un empleado con ese ID.")

    require_one_face(photo)
    key = f"referencias/{employee_id}/{uuid.uuid4().hex}.jpg"
    face_id = None
    uploaded = False

    try:
        aws()["s3"].put_object(
            Bucket=st.secrets["S3_BUCKET"],
            Key=key,
            Body=photo,
            ContentType="image/jpeg",
            ServerSideEncryption="AES256",
        )
        uploaded = True
        face_id = index_photo(employee_id, photo)

        item = {
            "empleado_id": employee_id,
            "nombre_completo": name,
            "cargo": position,
            "departamento": department,
            "estado": "ACTIVO",
            "face_id": face_id,
            "s3_key": key,
            "fecha_registro": iso_local(now_local()),
            "version": Decimal(0),
        }

        try:
            aws()["employees"].put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(empleado_id)",
            )
        except (ClientError, BotoCoreError):
            saved = get_employee(employee_id)
            if saved and saved.get("face_id") == face_id:
                return
            raise

    except Exception as exc:
        pending = cleanup_registration(
            face_id,
            key if uploaded else None,
        )
        if pending:
            raise BusinessError(
                "El alta falló y requiere conciliación manual de: "
                + ", ".join(pending)
            ) from exc
        raise


def deactivate_employee(employee_id):
    table = aws()["employees"]
    table.update_item(
        Key={"empleado_id": employee_id},
        UpdateExpression="SET #state = :inactive ADD #v :one",
        ConditionExpression="attribute_exists(empleado_id)",
        ExpressionAttributeNames={
            "#state": "estado",
            "#v": "version",
        },
        ExpressionAttributeValues={
            ":inactive": "INACTIVO",
            ":one": Decimal(1),
        },
    )

    employee = get_employee(employee_id)
    face_id = employee.get("face_id")
    if face_id:
        delete_face(face_id)
        table.update_item(
            Key={"empleado_id": employee_id},
            UpdateExpression="REMOVE face_id",
            ConditionExpression="#state = :inactive AND face_id = :face",
            ExpressionAttributeNames={"#state": "estado"},
            ExpressionAttributeValues={
                ":inactive": "INACTIVO",
                ":face": face_id,
            },
        )


def reactivate_employee(employee_id):
    employee = get_employee(employee_id)
    if not employee:
        raise BusinessError("Empleado inexistente.")
    if employee["estado"] == "ACTIVO":
        raise BusinessError("El empleado ya está activo.")

    if employee.get("face_id"):
        raise BusinessError(
            "Primero complete la eliminación pendiente del vector "
            "con el botón Inhabilitar / reintentar."
        )

    response = aws()["s3"].get_object(
        Bucket=st.secrets["S3_BUCKET"],
        Key=employee["s3_key"],
    )
    body = response["Body"]
    try:
        photo = body.read()
    finally:
        body.close()

    require_one_face(photo)
    face_id = index_photo(employee_id, photo)

    try:
        aws()["employees"].update_item(
            Key={"empleado_id": employee_id},
            UpdateExpression=(
                "SET #state = :active, face_id = :face ADD #v :one"
            ),
            ConditionExpression=(
                "#state = :inactive AND #v = :old "
                "AND attribute_not_exists(face_id)"
            ),
            ExpressionAttributeNames={
                "#state": "estado",
                "#v": "version",
            },
            ExpressionAttributeValues={
                ":active": "ACTIVO",
                ":inactive": "INACTIVO",
                ":face": face_id,
                ":one": Decimal(1),
                ":old": employee["version"],
            },
        )
    except (ClientError, BotoCoreError):
        current = get_employee(employee_id)
        if current and current.get("face_id") == face_id:
            return
        delete_face(face_id)
        raise


def recognize(photo):
    require_one_face(photo)
    threshold = float(st.secrets.get("FACE_MATCH_THRESHOLD", 95))
    if not 90 <= threshold <= 100:
        raise BusinessError("FACE_MATCH_THRESHOLD debe estar entre 90 y 100.")

    response = aws()["rek"].search_faces_by_image(
        CollectionId=st.secrets["REKOGNITION_COLLECTION"],
        Image={"Bytes": photo},
        FaceMatchThreshold=threshold,
        MaxFaces=1,
        QualityFilter="AUTO",
    )
    matches = response.get("FaceMatches", [])
    if not matches:
        raise BusinessError(
            "Rostro no reconocido. Contacte a Recursos Humanos."
        )

    match = matches[0]
    face = match["Face"]
    employee_id = face.get("ExternalImageId")
    employee = get_employee(employee_id) if employee_id else None

    if not employee:
        raise BusinessError(
            "Rostro sin perfil válido. Contacte a Recursos Humanos."
        )
    if employee.get("estado") != "ACTIVO":
        raise BusinessError("Acceso denegado: empleado INACTIVO.")
    if employee.get("face_id") != face["FaceId"]:
        raise BusinessError(
            "La referencia facial cambió. Capture una nueva fotografía."
        )

    return employee, Decimal(str(match["Similarity"]))


def record_attendance(employee, similarity):
    employee_id = employee["empleado_id"]
    current = now_local()
    last = latest_attendance(employee_id)

    if last:
        previous = datetime.fromisoformat(last["timestamp"])
        elapsed = (current - previous).total_seconds()
        if elapsed < 0:
            raise BusinessError(
                "Se detectó una inconsistencia de reloj. Contacte al administrador."
            )
        if elapsed < COOLDOWN_SECONDS:
            raise BusinessError(
                f"Marcaje ya procesado hace {int(elapsed)} segundos. "
                "Espere 2 minutos desde el último marcaje."
            )

    today = current.date().isoformat()
    event_type = "ENTRADA"
    event = {
        "empleado_id": employee_id,
        "timestamp": iso_local(current),
        "fecha": today,
        "tipo_evento": event_type,
        "similitud": similarity,
    }

    if (
        last
        and last["fecha"] == today
        and last["tipo_evento"] == "ENTRADA"
    ):
        delta = current - datetime.fromisoformat(last["timestamp"])
        microseconds = (
            (delta.days * 86400 + delta.seconds) * 1_000_000
            + delta.microseconds
        )
        seconds = Decimal(microseconds) / Decimal(1_000_000)
        event["tipo_evento"] = "SALIDA"
        event["segundos_trabajados"] = seconds
        event["horas_trabajadas"] = seconds / Decimal(3600)

    values = {
        ":active": "ACTIVO",
        ":face": employee["face_id"],
        ":version": employee["version"],
        ":one": Decimal(1),
        ":timestamp": event["timestamp"],
    }
    condition = (
        "#state = :active AND face_id = :face AND #v = :version"
    )
    previous_timestamp = employee.get("ultimo_timestamp")
    if previous_timestamp:
        condition += " AND ultimo_timestamp = :previous"
        values[":previous"] = previous_timestamp
    else:
        condition += " AND attribute_not_exists(ultimo_timestamp)"

    request_token = uuid.uuid4().hex
    transaction = [
        {
            "Update": {
                "TableName": st.secrets["EMPLEADOS_TABLE"],
                "Key": serialize({"empleado_id": employee_id}),
                "UpdateExpression": (
                    "SET ultimo_timestamp = :timestamp ADD #v :one"
                ),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {
                    "#state": "estado",
                    "#v": "version",
                },
                "ExpressionAttributeValues": serialize(values),
            }
        },
        {
            "Put": {
                "TableName": st.secrets["ASISTENCIAS_TABLE"],
                "Item": serialize(event),
                "ConditionExpression": (
                    "attribute_not_exists(empleado_id) "
                    "AND attribute_not_exists(#ts)"
                ),
                "ExpressionAttributeNames": {"#ts": "timestamp"},
            }
        },
    ]

    try:
        aws()["ddb"].transact_write_items(
            TransactItems=transaction,
            ClientRequestToken=request_token,
        )
    except (ClientError, BotoCoreError) as exc:
        confirmed = latest_attendance(employee_id)
        if confirmed and confirmed["timestamp"] == event["timestamp"]:
            return event

        if isinstance(exc, ClientError):
            code = exc.response["Error"]["Code"]
            if code in {
                "TransactionCanceledException",
                "TransactionConflictException",
            }:
                raise BusinessError(
                    "El estado cambió o hubo otro marcaje simultáneo. "
                    "Capture de nuevo; no se registró este intento."
                ) from exc
        raise

    return event


def show_error(exc):
    if isinstance(exc, BusinessError):
        st.warning(str(exc))
        return

    reference = uuid.uuid4().hex[:10]
    code = (
        exc.response["Error"]["Code"]
        if isinstance(exc, ClientError)
        else type(exc).__name__
    )
    LOGGER.error("AWS failure reference=%s code=%s", reference, code)
    st.error(
        f"No fue posible completar la operación. "
        f"Contacte al administrador. Referencia: {reference}"
    )


def kiosk():
    st.subheader("Recepción · Marcaje sin contacto")
    st.caption(
        "Una sola persona, rostro frontal y buena iluminación. "
        "Esta demo no incorpora prueba de vida."
    )

    st.session_state.setdefault("camera_generation", 0)
    key = f"kiosk_camera_{st.session_state.camera_generation}"

    col_camara, col_informacion = st.columns([1, 1.5])

    with col_camara:
        capture = st.camera_input(
            "Capture su fotografía",
            key=key,
        )

    with col_informacion:
        st.markdown("### Registro de asistencia")
        st.write("Colóquese de frente y procure tener buena iluminación.")
        st.write("Debe aparecer una sola persona en la fotografía.")
        st.caption(
            "La captura inicia automáticamente el reconocimiento. "
            "Espere la confirmación antes de continuar."
        )
    
    if capture is not None:
        digest = hashlib.sha256(capture.getvalue()).hexdigest()
        if digest != st.session_state.get("last_capture_digest"):
            st.session_state.last_capture_digest = digest
            try:
                with st.spinner("Verificando identidad y registrando..."):
                    photo = normalize_photo(capture)
                    employee, similarity = recognize(photo)
                    event = record_attendance(employee, similarity)
                    st.session_state.last_kiosk_result = {
                        "ok": True,
                        "name": employee["nombre_completo"],
                        "position": employee["cargo"],
                        "event": event,
                    }
            except (BusinessError, ClientError, BotoCoreError) as exc:
                if isinstance(exc, BusinessError):
                    message = str(exc)
                else:
                    reference = uuid.uuid4().hex[:10]
                    LOGGER.error(
                        "Kiosk AWS failure reference=%s type=%s",
                        reference,
                        type(exc).__name__,
                    )
                    message = (
                        "No fue posible procesar el marcaje. "
                        f"Referencia: {reference}"
                    )
                st.session_state.last_kiosk_result = {
                    "ok": False,
                    "message": message,
                }

    result = st.session_state.get("last_kiosk_result")
    if result:
        if not result["ok"]:
            st.warning(result["message"])
        else:
            event = result["event"]
            with st.container(border=True):
                st.success(f"Marcaje confirmado: {event['tipo_evento']}")
                st.write(result["name"])
                st.write(f"Cargo: {result['position']}")
                st.write(
                    "Hora local: "
                    + datetime.fromisoformat(event["timestamp"]).strftime(
                        "%d/%m/%Y %H:%M:%S"
                    )
                )
                st.write(f"Similitud: {event['similitud']:.2f}%")
                if "horas_trabajadas" in event:
                    st.write(
                        f"Horas del intervalo: "
                        f"{event['horas_trabajadas']:.4f}"
                    )

    if st.button("Limpiar y preparar siguiente persona"):
        st.session_state.camera_generation += 1
        st.session_state.pop("last_capture_digest", None)
        st.session_state.pop("last_kiosk_result", None)
        st.rerun()


def admin_authenticated():
    if st.session_state.get("admin_authenticated"):
        if st.button("Cerrar sesión administrativa"):
            st.session_state.admin_authenticated = False
            st.rerun()
        return True

    blocked_until = st.session_state.get("login_blocked_until", 0)
    if time.time() < blocked_until:
        st.warning("Demasiados intentos. Espere un minuto.")
        return False

    with st.form("admin_login", clear_on_submit=True):
        password = st.text_input("Contraseña de RR. HH.", type="password")
        submitted = st.form_submit_button("Ingresar")

    if submitted:
        expected = str(st.secrets["ADMIN_PASSWORD"])
        if hmac.compare_digest(
            password.encode("utf-8"),
            expected.encode("utf-8"),
        ):
            st.session_state.admin_authenticated = True
            st.session_state.login_failures = 0
            st.rerun()
        else:
            failures = st.session_state.get("login_failures", 0) + 1
            st.session_state.login_failures = failures
            if failures >= 5:
                st.session_state.login_blocked_until = time.time() + 60
                st.session_state.login_failures = 0
            st.error("Contraseña incorrecta.")

    return False


def registration_ui():
    st.subheader("Registro de empleados")
    with st.form("registration", clear_on_submit=True):
        employee_id = st.text_input("ID del empleado")
        name = st.text_input("Nombre completo")
        position = st.text_input("Cargo")
        department = st.text_input("Departamento")

        col_foto, col_archivo = st.columns([1, 1.5])

        with col_foto:
            camera = st.camera_input(
                "Fotografía por cámara",
                key="admin_camera",
            )

        with col_archivo:
            uploaded = st.file_uploader(
                "O seleccione una fotografía",
                type=["jpg", "jpeg", "png"],
            )

            st.caption(
                "Utilice cámara o archivo, no ambos. "
                "La fotografía debe mostrar una sola persona."
            )
        submitted = st.form_submit_button("Registrar empleado")

    if submitted:
        try:
            if camera is not None and uploaded is not None:
                raise BusinessError(
                    "Use solo una fuente: cámara o archivo."
                )
            source = camera if camera is not None else uploaded
            photo = normalize_photo(source)
            with st.spinner("Registrando empleado..."):
                register_employee(
                    employee_id, name, position, department, photo
                )
            st.success("Empleado registrado como ACTIVO.")
        except (BusinessError, ClientError, BotoCoreError) as exc:
            show_error(exc)


def employee_state_ui():
    st.subheader("Gestión de estado")
    employees = scan_all(aws()["employees"])
    if not employees:
        st.info("Todavía no hay empleados.")
        return

    employees.sort(key=lambda item: item["empleado_id"])
    columns = [
        "empleado_id",
        "nombre_completo",
        "cargo",
        "departamento",
        "estado",
    ]
    st.dataframe(
        pd.DataFrame(employees).reindex(columns=columns),
        hide_index=True,
        use_container_width=True,
    )

    by_id = {item["empleado_id"]: item for item in employees}
    selected = st.selectbox(
        "Empleado",
        list(by_id),
        format_func=lambda value: (
            f"{value} · {by_id[value]['nombre_completo']} "
            f"· {by_id[value]['estado']}"
        ),
    )
    confirm = st.checkbox(
        "Confirmo el cambio de estado del empleado seleccionado."
    )
    left, right = st.columns(2)

    if left.button("Inhabilitar / reintentar eliminación"):
        try:
            if not confirm:
                raise BusinessError("Confirme primero la operación.")
            deactivate_employee(selected)
            st.success(
                "Empleado INACTIVO y vector eliminado. "
                "Historial conservado. Actualice la vista."
            )
        except (BusinessError, ClientError, BotoCoreError) as exc:
            show_error(exc)

    if right.button("Reactivar e indexar fotografía"):
        try:
            if not confirm:
                raise BusinessError("Confirme primero la operación.")
            reactivate_employee(selected)
            st.success("Empleado reactivado. Actualice la vista.")
        except (BusinessError, ClientError, BotoCoreError) as exc:
            show_error(exc)

    if st.button("Actualizar vista de empleados"):
        st.rerun()


def csv_safe(value):
    if isinstance(value, str) and value.startswith(
        ("=", "+", "-", "@", "\t", "\r", "\n")
    ):
        return "'" + value
    return value


def reports_ui():
    st.subheader("Reportes y auditoría")
    employees = scan_all(aws()["employees"])
    records = scan_all(aws()["attendance"])

    active = sum(item["estado"] == "ACTIVO" for item in employees)
    today = now_local().date().isoformat()
    today_records = [item for item in records if item["fecha"] == today]

    first, second, third = st.columns(3)
    first.metric("Empleados activos", active)
    second.metric("Marcajes de hoy", len(today_records))
    third.metric(
        "Empleados con marcaje hoy",
        len({item["empleado_id"] for item in today_records}),
    )

    start_col, end_col = st.columns(2)
    start = start_col.date_input("Desde", value=now_local().date())
    end = end_col.date_input("Hasta", value=now_local().date())
    if start > end:
        st.warning("La fecha inicial no puede superar la final.")
        return

    names = {
        item["empleado_id"]: item["nombre_completo"]
        for item in employees
    }
    selected = st.selectbox(
        "Filtrar empleado",
        ["TODOS"] + sorted(names),
        format_func=lambda value: (
            "Todos los empleados"
            if value == "TODOS"
            else f"{value} · {names[value]}"
        ),
    )

    filtered = [
        item
        for item in records
        if start.isoformat() <= item["fecha"] <= end.isoformat()
        and (selected == "TODOS" or item["empleado_id"] == selected)
    ]
    filtered.sort(key=lambda item: item["timestamp"], reverse=True)

    total = sum(
        (item.get("horas_trabajadas", Decimal(0)) for item in filtered),
        Decimal(0),
    )
    st.metric("Horas cerradas del período filtrado", f"{total:.4f}")
    st.caption(
        "Solo se suman intervalos cerrados mediante SALIDA. "
        "Una ENTRADA abierta todavía no aporta horas."
    )

    if not filtered:
        st.info("No hay registros para estos filtros.")
        return

    rows = []
    for item in filtered:
        row = dict(item)
        row["nombre_completo"] = names.get(item["empleado_id"], "")
        for field in ("similitud", "horas_trabajadas", "segundos_trabajados"):
            if field in row:
                row[field] = float(row[field])
        rows.append(row)

    frame = pd.DataFrame(rows)
    ordered = [
        "empleado_id",
        "nombre_completo",
        "timestamp",
        "fecha",
        "tipo_evento",
        "similitud",
        "horas_trabajadas",
        "segundos_trabajados",
    ]
    frame = frame.reindex(columns=ordered)
    st.dataframe(frame, hide_index=True, use_container_width=True)

    export = frame.copy()
    for column in export.columns:
        export[column] = export[column].map(csv_safe)

    st.download_button(
        "Descargar CSV",
        data=export.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"asistencias_{start}_{end}.csv",
        mime="text/csv",
    )


def admin_panel():
    st.subheader("Recursos Humanos")
    if not admin_authenticated():
        return

    module = st.radio(
        "Módulo",
        ["Registro", "Estado", "Reportes"],
        horizontal=True,
    )
    try:
        if module == "Registro":
            registration_ui()
        elif module == "Estado":
            employee_state_ui()
        else:
            reports_ui()
    except (BusinessError, ClientError, BotoCoreError) as exc:
        show_error(exc)


def main():
    st.title("Asistencia Cloud")
    st.caption(
        "2026 · "
        "Hora oficial del sistema: America/Tegucigalpa"
    )

    kiosk_tab, admin_tab = st.tabs(
        ["Modo kiosco", "Panel administrativo"]
    )
    with kiosk_tab:
        kiosk()
    with admin_tab:
        admin_panel()


if __name__ == "__main__":
    main()