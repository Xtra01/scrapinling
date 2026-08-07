"""
Data models for LinkedIn profile scraping results.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import json


@dataclass
class WorkExperience:
    company: str = ""
    company_linkedin_url: str = ""
    title: str = ""
    description: str = ""
    location: str = ""
    starts_at_year: Optional[int] = None
    starts_at_month: Optional[int] = None
    starts_at_day: Optional[int] = None
    ends_at_year: Optional[int] = None
    ends_at_month: Optional[int] = None
    ends_at_day: Optional[int] = None
    is_current: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_pdl(cls, exp: dict) -> "WorkExperience":
        start = exp.get("start_date") or ""
        end = exp.get("end_date") or ""
        return cls(
            company=exp.get("company", {}).get("name") or "",
            company_linkedin_url=exp.get("company", {}).get("linkedin_url") or "",
            title=exp.get("title", {}).get("name") or "",
            description="",
            location=(exp.get("location_names") or [""])[0] or "",
            starts_at_year=int(start[:4]) if start[:4].isdigit() else None,
            ends_at_year=int(end[:4]) if end[:4].isdigit() else None,
            is_current=bool(exp.get("is_primary", False)),
        )

    def start_str(self) -> str:
        parts = [p for p in [self.starts_at_year, self.starts_at_month, self.starts_at_day] if p]
        return "-".join(str(p) for p in parts) if parts else ""

    def end_str(self) -> str:
        if self.is_current:
            return "Present"
        parts = [p for p in [self.ends_at_year, self.ends_at_month, self.ends_at_day] if p]
        return "-".join(str(p) for p in parts) if parts else ""


@dataclass
class Education:
    school: str = ""
    school_linkedin_url: str = ""
    degree: str = ""
    field_of_study: str = ""
    description: str = ""
    grade: str = ""
    activities: str = ""
    starts_at_year: Optional[int] = None
    ends_at_year: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_pdl(cls, edu: dict) -> "Education":
        start = edu.get("start_date") or ""
        end = edu.get("end_date") or ""
        return cls(
            school=edu.get("school", {}).get("name") or "",
            school_linkedin_url=edu.get("school", {}).get("linkedin_url") or "",
            degree=(edu.get("degrees") or [""])[0] or "",
            field_of_study=(edu.get("majors") or [""])[0] or "",
            grade=edu.get("gpa") or "",
            starts_at_year=int(start[:4]) if start[:4].isdigit() else None,
            ends_at_year=int(end[:4]) if end[:4].isdigit() else None,
        )


@dataclass
class LinkedInProfile:
    linkedin_url: str = ""
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    headline: str = ""
    summary: str = ""
    location: str = ""
    country: str = ""
    city: str = ""
    profile_pic_url: str = ""
    connections: Optional[int] = None
    follower_count: Optional[int] = None
    experiences: list[WorkExperience] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    source_api: str = ""
    fetch_status: str = "pending"  # pending | success | failed | not_found
    error_message: str = ""

    @classmethod
    def from_pdl(cls, url: str, data: dict) -> "LinkedInProfile":
        person = data.get("data") or data
        experiences = [WorkExperience.from_pdl(e) for e in (person.get("experience") or [])]
        education = [Education.from_pdl(e) for e in (person.get("education") or [])]
        languages = [
            (lang.get("name") if isinstance(lang, dict) else lang)
            for lang in (person.get("languages") or [])
        ]
        return cls(
            linkedin_url=url,
            full_name=person.get("full_name") or "",
            first_name=person.get("first_name") or "",
            last_name=person.get("last_name") or "",
            headline=person.get("job_title") or "",
            summary=person.get("summary") or "",
            location=person.get("location_name") or "",
            country=person.get("location_country") or "",
            city=person.get("location_locality") or "",
            profile_pic_url=person.get("profile_pic_url") or "",
            experiences=experiences,
            education=education,
            skills=person.get("skills") or [],
            languages=[l for l in languages if l],
            source_api="pdl",
            fetch_status="success",
        )

    def to_json(self) -> str:
        d = {
            "linkedin_url": self.linkedin_url,
            "full_name": self.full_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "headline": self.headline,
            "summary": self.summary,
            "location": self.location,
            "country": self.country,
            "city": self.city,
            "profile_pic_url": self.profile_pic_url,
            "connections": self.connections,
            "follower_count": self.follower_count,
            "experiences": [e.to_dict() for e in self.experiences],
            "education": [e.to_dict() for e in self.education],
            "skills": self.skills,
            "languages": self.languages,
            "source_api": self.source_api,
            "fetch_status": self.fetch_status,
            "error_message": self.error_message,
        }
        return json.dumps(d, ensure_ascii=False)

    def to_flat_rows(self) -> list[dict]:
        """Flatten profile into rows suitable for CSV (one row per experience + education combo)."""
        base = {
            "linkedin_url": self.linkedin_url,
            "full_name": self.full_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "headline": self.headline,
            "location": self.location,
            "country": self.country,
            "city": self.city,
            "connections": self.connections,
            "source_api": self.source_api,
            "fetch_status": self.fetch_status,
            "error_message": self.error_message,
        }

        if not self.experiences and not self.education:
            return [base]

        rows = []
        for i, exp in enumerate(self.experiences):
            row = dict(base)
            row["record_type"] = "experience"
            row["exp_index"] = i + 1
            row["exp_company"] = exp.company
            row["exp_company_linkedin_url"] = exp.company_linkedin_url
            row["exp_title"] = exp.title
            row["exp_description"] = exp.description
            row["exp_location"] = exp.location
            row["exp_start"] = exp.start_str()
            row["exp_end"] = exp.end_str()
            row["exp_is_current"] = exp.is_current
            row["edu_index"] = ""
            row["edu_school"] = ""
            row["edu_school_linkedin_url"] = ""
            row["edu_degree"] = ""
            row["edu_field_of_study"] = ""
            row["edu_grade"] = ""
            row["edu_start_year"] = ""
            row["edu_end_year"] = ""
            rows.append(row)

        for i, edu in enumerate(self.education):
            row = dict(base)
            row["record_type"] = "education"
            row["exp_index"] = ""
            row["exp_company"] = ""
            row["exp_company_linkedin_url"] = ""
            row["exp_title"] = ""
            row["exp_description"] = ""
            row["exp_location"] = ""
            row["exp_start"] = ""
            row["exp_end"] = ""
            row["exp_is_current"] = ""
            row["edu_index"] = i + 1
            row["edu_school"] = edu.school
            row["edu_school_linkedin_url"] = edu.school_linkedin_url
            row["edu_degree"] = edu.degree
            row["edu_field_of_study"] = edu.field_of_study
            row["edu_grade"] = edu.grade
            row["edu_start_year"] = edu.starts_at_year or ""
            row["edu_end_year"] = edu.ends_at_year or ""
            rows.append(row)

        return rows
