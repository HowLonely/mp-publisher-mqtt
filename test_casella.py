import serial
import time

PUERTO = '/dev/ttyACM0'
BAUD = 9600

def leer_datos():
    ser = serial.Serial(PUERTO, BAUD, timeout=1)
    print("Conexión establecida. Procesando datos...")

    # Limpiamos el buffer inicial
    ser.reset_input_buffer()

    while True:
        # Solicitamos datos constantemente
        ser.write(b'<D(?)>\r\n')

        # Leemos hasta encontrar el fin de línea
        linea = ser.readline().decode('utf-8', errors='ignore').strip()

        # Filtramos: Solo procesamos si la línea contiene DATA y termina en >
        if "DATA" in linea or "INS" in linea:
            # Aquí tienes tu string limpio para procesar
            print(f"Dato procesado: {linea}")

            # Ejemplo de cómo extraer el valor INS (Instantáneo)
            try:
                partes = linea.split(',')
                # Según el manual, INS es la posición 4
                val_ins = partes[4].strip()
                print(f"-> Concentración actual: {val_ins} mg/m3")
            except IndexError:
                pass

        time.sleep(1)

if __name__ == "__main__":
    try:
        leer_datos()
    except KeyboardInterrupt:
        print("Monitoreo detenido por usuario.")
