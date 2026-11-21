from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from uuid import UUID
import datetime

if TYPE_CHECKING:
  from ..models.contract_validation_report_out_pins_item import ContractValidationReportOutPinsItem
  from ..models.contract_validation_report_out_report import ContractValidationReportOutReport





T = TypeVar("T", bound="ContractValidationReportOut")



@_attrs_define
class ContractValidationReportOut:
    """ The stored validation report a version was published with (D-155, VAL-09).

    ``report`` is validation's D-128 report, passed through as JSON with its ``compatibility`` and
    ``ref_facts`` sections; ``pins`` are the rule-set versions it was validated against.

        Attributes:
            content_sha256 (str):
            contract_id (UUID):
            dsl_version (str):
            engine_version (str):
            pins (list[ContractValidationReportOutPinsItem]):
            report (ContractValidationReportOutReport):
            run_id (UUID):
            schema_sha (str):
            semver (str):
            validated_at (datetime.datetime):
            version_id (UUID):
     """

    content_sha256: str
    contract_id: UUID
    dsl_version: str
    engine_version: str
    pins: list[ContractValidationReportOutPinsItem]
    report: ContractValidationReportOutReport
    run_id: UUID
    schema_sha: str
    semver: str
    validated_at: datetime.datetime
    version_id: UUID
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.contract_validation_report_out_pins_item import ContractValidationReportOutPinsItem # noqa: PLC0415
        from ..models.contract_validation_report_out_report import ContractValidationReportOutReport # noqa: PLC0415
        content_sha256 = self.content_sha256

        contract_id = str(self.contract_id)

        dsl_version = self.dsl_version

        engine_version = self.engine_version

        pins = []
        for pins_item_data in self.pins:
            pins_item = pins_item_data.to_dict()
            pins.append(pins_item)



        report = self.report.to_dict()

        run_id = str(self.run_id)

        schema_sha = self.schema_sha

        semver = self.semver

        validated_at = self.validated_at.isoformat()

        version_id = str(self.version_id)


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "content_sha256": content_sha256,
            "contract_id": contract_id,
            "dsl_version": dsl_version,
            "engine_version": engine_version,
            "pins": pins,
            "report": report,
            "run_id": run_id,
            "schema_sha": schema_sha,
            "semver": semver,
            "validated_at": validated_at,
            "version_id": version_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.contract_validation_report_out_pins_item import ContractValidationReportOutPinsItem # noqa: PLC0415
        from ..models.contract_validation_report_out_report import ContractValidationReportOutReport # noqa: PLC0415
        d = dict(src_dict)
        content_sha256 = d.pop("content_sha256")

        contract_id = UUID(d.pop("contract_id"))




        dsl_version = d.pop("dsl_version")

        engine_version = d.pop("engine_version")

        pins = []
        _pins = d.pop("pins")
        for pins_item_data in (_pins):
            pins_item = ContractValidationReportOutPinsItem.from_dict(pins_item_data)



            pins.append(pins_item)


        report = ContractValidationReportOutReport.from_dict(d.pop("report"))




        run_id = UUID(d.pop("run_id"))




        schema_sha = d.pop("schema_sha")

        semver = d.pop("semver")

        validated_at = datetime.datetime.fromisoformat(d.pop("validated_at"))




        version_id = UUID(d.pop("version_id"))




        contract_validation_report_out = cls(
            content_sha256=content_sha256,
            contract_id=contract_id,
            dsl_version=dsl_version,
            engine_version=engine_version,
            pins=pins,
            report=report,
            run_id=run_id,
            schema_sha=schema_sha,
            semver=semver,
            validated_at=validated_at,
            version_id=version_id,
        )


        contract_validation_report_out.additional_properties = d
        return contract_validation_report_out

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
