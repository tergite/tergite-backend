# This code is part of Tergite
#
# (C) Copyright David Wahlstedt 2023
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import logging
from typing import Optional

import toml

from backend_properties_storage.storage import (
    BackendProperty,
    PropertyType,
    set_component_property,
)

"""Logging initialization"""

logger = logging.getLogger(__name__)
FORMAT = "[%(filename)s:%(lineno)s - %(funcName)20s() ] %(message)s"
logging.basicConfig(format=FORMAT)
# The following two lines are not used yet, but can be good to have available:
logger.setLevel(logging.INFO)
LOGLEVEL = logging.INFO


# =============================================================================
# Create empty Redis entries with pre-defined backend properties

# Question: where should this filepath be defined? .env?
# For testing purposes it is good if this path can be configured.
TEMPLATE_FILE = "backend_properties_config/property_templates_default.toml"

# precondition: device layout configuration has been loaded,
# which means: we know which components are present, and in what numbers
def initialize_properties() -> bool:
    try:
        property_template = toml.load(TEMPLATE_FILE)
        # The section's name is the same as the property type's str value:
        device_template = property_template[str(PropertyType.DEVICE)]
        components = BackendProperty.read_value(
            property_type=PropertyType.DEVICE, name="components"
        )
        for tag, tag_dict in device_template.items():
            if tag in components:  # then tag is a component property
                for name, fields in tag_dict.items():
                    n_components = get_n_components(tag)
                    # Populate Redis from template for each component index
                    for index in range(n_components):
                        set_component_property(
                            name=name,
                            component=tag,
                            index=index,
                            value=None,  # serves as a placeholder in Redis
                            source="default",
                            **fields,
                        )
            else:
                # otherwise tag is a device property name
                BackendProperty(
                    property_type=PropertyType.DEVICE,
                    name=tag,
                    **tag_dict,
                ).write()
    except Exception as error:
        logger.error(f"Failed to initialize properties: {error=}")
        return False
    return True


def set_n_components(component: str, n: int):
    property_name = f"number_of_{component}s"
    BackendProperty(
        property_type=PropertyType.DEVICE,
        name=property_name,
        value=n,
        source="configuration",
    ).write()


def get_n_components(component: str) -> Optional[int]:
    property_name = f"number_of_{component}s"
    return BackendProperty.read_value(
        property_type=PropertyType.DEVICE, name=property_name
    )


# =============================================================================
# Main

if __name__ == "__main__":
    # Requires number_of_resonators to be set in Redis
    initialize_properties()