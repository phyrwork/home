import homeassistant.helpers.area_registry as area_registry
import homeassistant.helpers.entity_registry as entity_registry


@service
async def set_entity_area(entity_id=None, area_id=None, area_name=None):
    if not entity_id:
        log.error("set_entity_area: missing entity_id")
        return

    if not area_id and area_name:
        registry = area_registry.async_get(hass)
        for area in registry.async_list_areas():
            if area.name == area_name:
                area_id = area.id
                break

    if not area_id:
        log.error("set_entity_area: missing area_id for %s", entity_id)
        return

    registry = entity_registry.async_get(hass)
    registry.async_update_entity(entity_id, area_id=area_id)
