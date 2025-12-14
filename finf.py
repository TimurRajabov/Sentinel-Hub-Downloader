import json

GEOJSON_PATH = (
    "/home/temur/Documents/Work/goo/geojson/uzbekistan_regions_backup.geojson"
)

# Загружаем GeoJSON
with open(GEOJSON_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

print("\n=========== СПИСОК ВСЕХ REGION_SOATO ==========\n")

for feature in data["features"]:
    props = feature["properties"]

    print("-------------------------------------------------------")
    print("shapeName:", props.get("shapeName"))
    print("shapeISO:", props.get("shapeISO"))
    print("shapeID:", props.get("shapeID"))
    print("shapeGroup:", props.get("shapeGroup"))
    print("shapeType:", props.get("shapeType"))
    print("region_soato:", props.get("region_soato"))
    print("-------------------------------------------------------\n")
