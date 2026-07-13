from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: Optional[str] = Field(None, min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)
    phone: Optional[str] = None
    login_type: str = Field("username", pattern=r"^(username|phone)$")

    @model_validator(mode="after")
    def _validate_identifier(self) -> "LoginRequest":
        if self.login_type == "phone":
            if not self.phone:
                raise ValueError("login_type=phone 时必须提供 phone")
        else:
            if not self.username:
                raise ValueError("login_type=username 时必须提供 username")
        return self


class RegisterRequest(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=128)
    email: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None

    @field_validator("phone", mode="before")
    @classmethod
    def _empty_phone_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _ensure_username(self) -> "RegisterRequest":
        if not self.username:
            if self.name:
                self.username = self.name
            elif self.phone:
                self.username = f"用户{self.phone[-4:]}"
        if not self.username or len(self.username) < 3:
            raise ValueError("用户名至少3个字符（可通过 username、name 或 phone 自动生成）")
        return self


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., description="刷新令牌")


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class UpdateUserRequest(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    old_password: Optional[str] = None
    new_password: Optional[str] = Field(None, min_length=8, max_length=128)

    @field_validator("phone", mode="before")
    @classmethod
    def _empty_phone_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _validate_passwords(self) -> "UpdateUserRequest":
        if self.new_password and not self.old_password:
            raise ValueError("修改密码时必须提供旧密码")
        return self
