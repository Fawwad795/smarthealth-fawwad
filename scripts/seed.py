"""Seed script: a realistic synthetic demo dataset in one command.

Run with: docker compose exec api python -m scripts.seed

Builds, in dependency order: a clinic, departments, specialties, staff
accounts (provider/front_desk/admin -- the only way those roles get
created, since POST /auth/register is patient-only by design), provider
profiles, provider schedules, generated slots, published services,
provider-service links, and a handful of synthetic patients.

Idempotent: every step checks for an existing row by its natural key
(email, name, weekday) before inserting, so running this twice against a
database that already has seed data is a no-op, not a crash.

Appointments are deliberately NOT seeded here -- the Appointment model
does not exist yet (it is Week 2's scheduling saga). Seeding rows against
a table that will be built next week would just be thrown away.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import (
    Clinic,
    Department,
    Patient,
    Provider,
    ProviderSchedule,
    ProviderService,
    Service,
    Specialty,
    User,
)
from app.models.enums import ServiceStatus, UserRole
from app.schemas.department import DepartmentCreate
from app.schemas.provider import ProviderCreate
from app.schemas.provider_schedule import ProviderScheduleCreate
from app.schemas.service import ServiceCreate
from app.services import department as department_service
from app.services import provider as provider_service
from app.services import provider_schedule as provider_schedule_service
from app.services import service as service_service

# Synthetic-only, never used outside local/dev seeding -- see rule 9.
SEED_PASSWORD = "ChangeMe123!"


def _get_or_create_clinic(db: Session) -> Clinic:
    clinic = db.query(Clinic).filter(Clinic.name == "MediNova Central").first()
    if clinic is not None:
        return clinic
    clinic = Clinic(name="MediNova Central", timezone="Asia/Karachi")
    db.add(clinic)
    db.flush()
    return clinic


def _get_or_create_department(db: Session, clinic: Clinic, name: str) -> Department:
    department = (
        db.query(Department)
        .filter(Department.clinic_id == clinic.id, Department.name == name)
        .first()
    )
    if department is not None:
        return department
    return department_service.create_department(
        db, DepartmentCreate(clinic_id=clinic.id, name=name)
    )


def _get_or_create_specialty(db: Session, name: str) -> Specialty:
    specialty = db.query(Specialty).filter(Specialty.name == name).first()
    if specialty is not None:
        return specialty
    specialty = Specialty(name=name)
    db.add(specialty)
    db.flush()
    return specialty


def _get_or_create_staff_user(db: Session, email: str, role: UserRole) -> User:
    email = email.lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is not None:
        return user
    user = User(email=email, password_hash=hash_password(SEED_PASSWORD), role=role)
    db.add(user)
    db.flush()
    return user


def _get_or_create_provider(
    db: Session, user: User, department: Department, specialty: Specialty, bio: str
) -> Provider:
    provider = db.query(Provider).filter(Provider.user_id == user.id).first()
    if provider is not None:
        return provider
    return provider_service.create_provider(
        db,
        ProviderCreate(
            user_id=user.id, department_id=department.id, specialty_id=specialty.id, bio=bio
        ),
    )


def _get_or_create_weekday_schedule(db: Session, provider: Provider, weekday: int) -> None:
    exists = (
        db.query(ProviderSchedule)
        .filter_by(provider_id=provider.id, weekday=weekday)
        .first()
    )
    if exists is not None:
        return
    provider_schedule_service.create_provider_schedule(
        db,
        provider.id,
        ProviderScheduleCreate(
            weekday=weekday, start_time="09:00", end_time="17:00", slot_duration_minutes=30
        ),
    )


def _get_or_create_published_service(
    db: Session, department: Department, name: str, description: str, prep: str
) -> Service:
    service = (
        db.query(Service)
        .filter(Service.department_id == department.id, Service.name == name)
        .first()
    )
    if service is not None:
        return service
    service = service_service.create_service(
        db,
        ServiceCreate(
            department_id=department.id, name=name, description=description, prep_instructions=prep
        ),
    )
    # No publish workflow exists yet (Week 2) -- this is exactly the
    # "hand-inserted row" escape hatch the CRUD endpoint's own docstring
    # anticipates for getting a demoable PUBLISHED row today.
    service.status = ServiceStatus.PUBLISHED
    service.published_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(service)
    return service


def _get_or_create_provider_service(db: Session, provider: Provider, service: Service) -> None:
    exists = (
        db.query(ProviderService)
        .filter_by(provider_id=provider.id, service_id=service.id)
        .first()
    )
    if exists is not None:
        return
    db.add(ProviderService(provider_id=provider.id, service_id=service.id))
    db.commit()


def _get_or_create_patient(db: Session, email: str, dob: date) -> Patient:
    email = email.lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is None:
        user = User(email=email, password_hash=hash_password(SEED_PASSWORD), role=UserRole.PATIENT)
        db.add(user)
        db.flush()
    patient = db.query(Patient).filter(Patient.user_id == user.id).first()
    if patient is not None:
        return patient
    patient = Patient(user_id=user.id, dob=dob)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def seed(db: Session) -> None:
    clinic = _get_or_create_clinic(db)

    cardiology_dept = _get_or_create_department(db, clinic, "Cardiology")
    ortho_dept = _get_or_create_department(db, clinic, "Orthopaedics")
    derm_dept = _get_or_create_department(db, clinic, "Dermatology")

    cardiology_spec = _get_or_create_specialty(db, "Cardiology")
    ortho_spec = _get_or_create_specialty(db, "Orthopedics")
    derm_spec = _get_or_create_specialty(db, "Dermatology")

    _get_or_create_staff_user(db, "admin@medinova.example", UserRole.ADMIN)
    _get_or_create_staff_user(db, "frontdesk@medinova.example", UserRole.FRONT_DESK)

    khan_user = _get_or_create_staff_user(db, "dr.khan@medinova.example", UserRole.PROVIDER)
    khan = _get_or_create_provider(
        db, khan_user, cardiology_dept, cardiology_spec, "Consultant cardiologist."
    )

    ahmed_user = _get_or_create_staff_user(db, "dr.ahmed@medinova.example", UserRole.PROVIDER)
    ahmed = _get_or_create_provider(
        db, ahmed_user, ortho_dept, ortho_spec, "Consultant orthopaedic surgeon."
    )

    raza_user = _get_or_create_staff_user(db, "dr.raza@medinova.example", UserRole.PROVIDER)
    raza = _get_or_create_provider(
        db, raza_user, derm_dept, derm_spec, "Consultant dermatologist."
    )

    for provider in (khan, ahmed, raza):
        for weekday in (0, 2, 4):  # Monday, Wednesday, Friday
            _get_or_create_weekday_schedule(db, provider, weekday)

    today = date.today()
    for provider in (khan, ahmed, raza):
        provider_schedule_service.generate_slots(db, provider.id, today, today + timedelta(days=14))

    knee_xray = _get_or_create_published_service(
        db,
        ortho_dept,
        "Knee X-Ray",
        "Standard imaging of the knee joint.",
        "Wear loose clothing; remove any metal jewellery near the knee.",
    )
    echo = _get_or_create_published_service(
        db,
        cardiology_dept,
        "Echocardiogram",
        "Ultrasound imaging of the heart.",
        "No special preparation required. Arrive 10 minutes early.",
    )
    skin_check = _get_or_create_published_service(
        db,
        derm_dept,
        "Full Skin Check",
        "A full-body skin examination.",
        "Avoid wearing makeup or nail polish to the appointment.",
    )

    _get_or_create_provider_service(db, ahmed, knee_xray)
    _get_or_create_provider_service(db, khan, echo)
    _get_or_create_provider_service(db, raza, skin_check)

    _get_or_create_patient(db, "patient.one@example.com", date(1990, 5, 14))
    _get_or_create_patient(db, "patient.two@example.com", date(1985, 11, 2))
    _get_or_create_patient(db, "patient.three@example.com", date(2000, 1, 30))


def main() -> None:
    db = SessionLocal()
    try:
        seed(db)
        print("Seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
