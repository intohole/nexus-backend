from typing import Annotated, Optional

from fastapi import Depends

from .auth import (
    get_current_user_full_normalized,
    get_current_user_id_optional,
    get_current_user_id_required,
)

UserIdRequired = Annotated[str, Depends(get_current_user_id_required)]
UserIdOptional = Annotated[Optional[str], Depends(get_current_user_id_optional)]
UserFullNormalized = Annotated[dict[str, object], Depends(get_current_user_full_normalized)]

__all__ = ["UserFullNormalized", "UserIdOptional", "UserIdRequired"]
