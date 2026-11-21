# Asistencia Cloud

Sistema de asistencia sin contacto desarrollado con Python, Streamlit y servicios administrados de AWS.

## Descripción

La aplicación permite registrar empleados mediante una fotografía de referencia e identificar sus rostros para registrar entradas y salidas.

Incluye una seccion de marcaje y un panel administrativo protegido por contraseña. Los perfiles y eventos se almacenan en DynamoDB, las fotografías de referencia en un bucket privado de S3 y los vectores faciales en una colección de Amazon Rekognition.

## Funcionalidades

### Kiosco de asistencia

- Captura de fotografía mediante cámara.
- Reconocimiento de empleados registrados.
- Validación de un solo rostro por imagen.
- Umbral de similitud configurable, con un mínimo de 90.
- Registro automático de ENTRADA y SALIDA.
- Bloqueo de marcajes repetidos durante 120 segundos.
- Cálculo de horas de los intervalos cerrados.
- Confirmación con nombre, cargo, hora local y similitud.

### Panel administrativo

- Acceso mediante contraseña.
- Registro de empleados con cámara o archivo JPG/PNG.
- Inhabilitación sin eliminar el historial de asistencia.
- Eliminación del vector facial al inhabilitar.
- Reactivación mediante reindexación de la fotografía de referencia.
- Indicadores, filtros y descarga de reportes CSV.

## AWS

### DynamoDB

| Tabla | Partition key | Sort key | Capacidad |
|---|---|---|---|
| Empleados | empleado_id, String | No utiliza | On-Demand |
| Asistencias | empleado_id, String | timestamp, String | On-Demand |

Los demás atributos se crean cuando la aplicación guarda los registros.

### Rekognition

Crea una colección llamada asistencia-facial en la región seleccionada.

Crear el ARN en una política IAM no crea la colección.

### IAM

La política de ejecución debe adjuntarse al usuario o rol utilizado por
la aplicación. No debe pegarse en el editor de política del bucket.

La aplicación necesita:

- DetectFaces.
- IndexFaces, SearchFacesByImage y DeleteFaces sobre su colección.
- Lectura, escritura y eliminación de las fotografías de referencia.
- Operaciones necesarias sobre las dos tablas DynamoDB.

La identidad de ejecución no necesita permisos administrativos ni permisos
para crear infraestructura.

### Aprovisionamiento automatizado opcional

Si los recursos ya fueron creados manualmente, no es necesario ejecutar
setup_aws.py.

Para crearlos mediante el script, utiliza una identidad de aprovisionamiento
autorizada:

```powershell
python setup_aws.py --profile asistencia-setup --region us-east-1 --bucket TU_BUCKET_UNICO --collection asistencia-facial
```

El script modifica configuración del bucket y puede modificar el modo de
capacidad de las tablas. Utilízalo únicamente sobre recursos dedicados
al proyecto.

## Ejecución local

### 1. Configura una identidad de aprovisionamiento en AWS CLI

aws configure --profile asistencia-setup

Completa:

- Access key ID de la identidad de preparación.
- Secret access key correspondiente.
- Región `us-east-1`.
- Formato `json`.

### 2. Crear entorno virtual

Desde la carpeta del proyecto:

```powershell
python -m venv .venv
```

### 3. Instalar dependencias

Puedes utilizar directamente el intérprete del entorno virtual:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. Configurar secretos

Crea el archivo .streamlit/secrets.toml con esta estructura y completa
los valores en tu equipo:

```toml
ADMIN_PASSWORD = "ELIGE_UNA_CONTRASENA_NUEVA_Y_UNICA"

AWS_REGION = "us-east-1"
S3_BUCKET = "TU_BUCKET"
REKOGNITION_COLLECTION = "asistencia-facial"

EMPLEADOS_TABLE = "Empleados"
ASISTENCIAS_TABLE = "Asistencias"

FACE_MATCH_THRESHOLD = 95.0

AWS_ACCESS_KEY_ID = "TU_ACCESS_KEY_ID"
AWS_SECRET_ACCESS_KEY = "TU_SECRET_ACCESS_KEY"
```

Cada variable debe aparecer una sola vez.

ADMIN_PASSWORD es una contraseña independiente para el panel de Recursos
Humanos. No es la contraseña root de AWS ni la clave secreta IAM.

No agregues encabezados como [aws] si utilizas la versión que consulta los
valores directamente con st.secrets["NOMBRE"].

### 4. Iniciar configuración de Amazon AWS

Ejecuta:

```powershell
.\.venv\Scripts\python.exe setup_aws.py --profile asistencia-setup --region us-east-1 --bucket TU_BUCKET_UNICO --collection asistencia-facial
```
Sustituye `TU_BUCKET_UNICO`.

### 5. Iniciar la aplicación

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Abre la dirección indicada por Streamlit y permite el acceso a la cámara.

## Uso

### Registrar un empleado

1. Abre Panel administrativo.
2. Ingresa la contraseña configurada en ADMIN_PASSWORD.
3. Selecciona Registro.
4. Completa ID, nombre, cargo y departamento.
5. Captura o sube una fotografía con exactamente un rostro.
6. Presiona Registrar empleado.

La aplicación guarda la fotografía en S3, indexa el rostro y crea el perfil.

### Marcar asistencia

1. Abre Modo kiosco.
2. Captura una fotografía.
3. Espera la confirmación.
4. El primer evento del día será ENTRADA.
5. Un nuevo intento antes de 120 segundos se bloquea.
6. Después del cooldown, el siguiente evento será SALIDA si el anterior
   del mismo día fue ENTRADA.

### Inhabilitar

Desde Estado, selecciona al empleado y confirma su inhabilitación.

El historial se conserva y el vector se elimina. Después de eliminarlo,
el mensaje esperado en el kiosco es “Rostro no reconocido”.

Para reactivar al empleado se vuelve a indexar su fotografía de referencia.

### Reportes

Selecciona fechas y empleado para consultar registros y descargar CSV.

Las horas acumuladas incluyen únicamente intervalos cerrados mediante SALIDA.

