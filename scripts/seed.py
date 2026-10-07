"""Seed script: a realistic synthetic demo dataset in one command.

Run with: docker compose exec api python -m scripts.seed

Builds, in dependency order: a clinic, departments, specialties, staff
accounts (provider/front_desk/admin -- the only way those roles get
created, since POST /auth/register is patient-only by design), provider
profiles, provider schedules, generated slots, DRAFT services,
provider-service links, and a handful of synthetic patients. Then it
publishes the services through the real publish workflow, so the
temporal and temporal-worker containers must be running.

Idempotent: every step checks for an existing row by its natural key
(email, name, weekday) before inserting, and an already-published service
is not published again, so running this twice against a database that
already has seed data is a no-op, not a crash.

Appointments are deliberately NOT seeded here -- the Appointment model
does not exist yet (it is Week 2's scheduling saga). Seeding rows against
a table that will be built next week would just be thrown away.
"""

import asyncio
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session
from temporalio.client import Client

from app.core.exceptions import AppError
from app.core.security import hash_password, verify_password
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
from app.models.enums import UserRole
from app.schemas.department import DepartmentCreate
from app.schemas.provider import ProviderCreate
from app.schemas.provider_schedule import ProviderScheduleCreate
from app.schemas.service import ServiceCreate
from app.services import department as department_service
from app.services import provider as provider_service
from app.services import provider_schedule as provider_schedule_service
from app.services import service as service_service
from app.services import service_publish
from app.temporal.client import get_temporal_client

# Synthetic-only, never used outside local/dev seeding -- see rule 9.
SEED_PASSWORD = "ChangeMe123!"

# How long main() waits for one service's publish workflow. A publish takes
# a few seconds; the limit only exists so a stopped worker fails the seed
# with a message instead of hanging it.
PUBLISH_TIMEOUT_SECONDS = 120

# Every account this script owns, for the summary main() prints. Day 3 lost
# time trying SEED_PASSWORD against a hand-registered leftover that looked
# exactly like seed data; a printed list is the cheapest way to tell the two
# apart. A test asserts this list matches what seed() actually inserts, so it
# cannot quietly rot.
SEED_ACCOUNTS = (
    "admin@medinova.example",
    "frontdesk@medinova.example",
    "dr.khan@medinova.example",
    "dr.ahmed@medinova.example",
    "dr.raza@medinova.example",
    "patient.one@example.com",
    "patient.two@example.com",
    "patient.three@example.com",
)


def _reset_seed_password(db: Session, user: User) -> None:
    """Put a seed-owned account back on SEED_PASSWORD if it has drifted.

    A get-or-create that returns early on an existing row cannot repair
    one -- so a seeded login that stopped working would stay broken
    through every re-run, and re-running the seed is the obvious thing to
    reach for. Re-asserting makes the seed converge on its declared state
    rather than merely decline to duplicate it: the same property the
    analytics rollup was given on Day 1.

    Verified before rehashing because bcrypt is deliberately slow and the
    common case is a hash that is already correct. The caller must commit
    -- this only flushes.
    """
    try:
        if verify_password(SEED_PASSWORD, user.password_hash):
            return
    except ValueError:
        # A hash passlib cannot even parse is still a hash that is not
        # SEED_PASSWORD -- and it is the case most in need of repair.
        # The first version let this propagate, so the seed crashed on
        # exactly the row it existed to fix.
        pass
    user.password_hash = hash_password(SEED_PASSWORD)
    db.flush()


def _get_or_create_clinic(db: Session) -> Clinic:
    """The single clinic everything else hangs off. Inserted directly:
    there is no clinic CRUD endpoint, and a single-clinic system does not
    need one."""
    clinic = db.query(Clinic).filter(Clinic.name == "MediNova Central").first()
    if clinic is not None:
        return clinic
    clinic = Clinic(name="MediNova Central", timezone="Asia/Karachi")
    db.add(clinic)
    db.flush()
    return clinic


def _get_or_create_department(db: Session, clinic: Clinic, name: str) -> Department:
    """One department, via the real service function so the seeded data
    goes through the same validation an admin's request would."""
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
    """One specialty. Inserted directly -- Week 1 built no specialty CRUD,
    since the vocabulary is fixed rather than user-managed."""
    specialty = db.query(Specialty).filter(Specialty.name == name).first()
    if specialty is not None:
        return specialty
    specialty = Specialty(name=name)
    db.add(specialty)
    db.flush()
    return specialty


def _get_or_create_staff_user(db: Session, email: str, role: UserRole) -> User:
    """A provider, front_desk or admin account.

    This function is the *only* way those roles come into existence:
    POST /auth/register is patient-only precisely so no public route can
    mint one. Looked up on lower(email) to match the unique index.
    """
    email = email.lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is not None:
        _reset_seed_password(db, user)
        return user
    user = User(email=email, password_hash=hash_password(SEED_PASSWORD), role=role)
    db.add(user)
    db.flush()
    return user


def _get_or_create_provider(
    db: Session, user: User, department: Department, specialty: Specialty, bio: str
) -> Provider:
    """The operational profile for an already-created PROVIDER account."""
    provider = db.query(Provider).filter(Provider.user_id == user.id).first()
    if provider is not None:
        return provider
    return provider_service.create_provider(
        db,
        ProviderCreate(
            user_id=user.id,
            department_id=department.id,
            specialty_id=specialty.id,
            bio=bio,
        ),
    )


def _get_or_create_weekday_schedule(
    db: Session, provider: Provider, weekday: int
) -> None:
    """A 09:00-17:00 clinic-local window on one weekday, in 30-minute
    slots. Existence is checked on (provider, weekday) rather than the
    full window, so re-running never adds a second window to a day."""
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
            weekday=weekday,
            start_time="09:00",
            end_time="17:00",
            slot_duration_minutes=30,
        ),
    )


def _get_or_create_service(
    db: Session, department: Department, name: str, description: str, prep: str
) -> Service:
    """A service, created through the service layer and left as DRAFT.

    seed() never publishes anything itself: the publish workflow is the
    only path to PUBLISHED, because it is also what writes the service's
    chunk. Week 1 set PUBLISHED here directly, before that workflow
    existed, and left the seeded services with nothing for Week 4's
    search to find. main() publishes them through the workflow instead.
    """
    service = (
        db.query(Service)
        .filter(Service.department_id == department.id, Service.name == name)
        .first()
    )
    if service is not None:
        return service
    return service_service.create_service(
        db,
        ServiceCreate(
            department_id=department.id,
            name=name,
            description=description,
            prep_instructions=prep,
        ),
    )


def _get_or_create_provider_service(
    db: Session, provider: Provider, service: Service
) -> None:
    """Record that this provider is qualified to deliver this service.

    What the catalogue's specialty filter and "has available slots"
    filter both traverse -- without these links a published service is
    invisible to either.
    """
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
    """A synthetic patient: the User account and its Patient profile.

    Names and dates of birth here are invented -- rule 9, never real
    personal data in seeds or tests. Handles the half-created case (user
    exists, profile doesn't) so a re-run after an interrupted one
    completes rather than crashing.
    """
    email = email.lower()
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(SEED_PASSWORD),
            role=UserRole.PATIENT,
        )
        db.add(user)
        db.flush()
    else:
        _reset_seed_password(db, user)
    patient = db.query(Patient).filter(Patient.user_id == user.id).first()
    if patient is not None:
        return patient
    patient = Patient(user_id=user.id, dob=dob)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def seed(db: Session) -> list[Service]:
    """Build the whole demo dataset, in dependency order, and return the
    seeded services for main() to publish.

    Split from main() so the test suite can call it against the test
    database with its own Session, rather than shelling out to a
    subprocess and losing the transaction rollback that keeps tests
    isolated. It needs no Temporal server for the same reason: publishing
    happens afterwards, in publish_services().
    """
    clinic = _get_or_create_clinic(db)

    cardiology_dept = _get_or_create_department(db, clinic, "Cardiology")
    ortho_dept = _get_or_create_department(db, clinic, "Orthopaedics")
    derm_dept = _get_or_create_department(db, clinic, "Dermatology")

    cardiology_spec = _get_or_create_specialty(db, "Cardiology")
    ortho_spec = _get_or_create_specialty(db, "Orthopedics")
    derm_spec = _get_or_create_specialty(db, "Dermatology")

    _get_or_create_staff_user(db, "admin@medinova.example", UserRole.ADMIN)
    _get_or_create_staff_user(db, "frontdesk@medinova.example", UserRole.FRONT_DESK)

    khan_user = _get_or_create_staff_user(
        db, "dr.khan@medinova.example", UserRole.PROVIDER
    )
    khan = _get_or_create_provider(
        db, khan_user, cardiology_dept, cardiology_spec, "Consultant cardiologist."
    )

    ahmed_user = _get_or_create_staff_user(
        db, "dr.ahmed@medinova.example", UserRole.PROVIDER
    )
    ahmed = _get_or_create_provider(
        db, ahmed_user, ortho_dept, ortho_spec, "Consultant orthopaedic surgeon."
    )

    raza_user = _get_or_create_staff_user(
        db, "dr.raza@medinova.example", UserRole.PROVIDER
    )
    raza = _get_or_create_provider(
        db, raza_user, derm_dept, derm_spec, "Consultant dermatologist."
    )

    for provider in (khan, ahmed, raza):
        for weekday in (0, 2, 4):  # Monday, Wednesday, Friday
            _get_or_create_weekday_schedule(db, provider, weekday)

    today = date.today()
    for provider in (khan, ahmed, raza):
        provider_schedule_service.generate_slots(
            db, provider.id, today, today + timedelta(days=14)
        )

    knee_xray = _get_or_create_service(
        db,
        ortho_dept,
        "Knee X-Ray",
        "Standard imaging of the knee joint.",
        "Wear loose clothing; remove any metal jewellery near the knee.",
    )
    echo = _get_or_create_service(
        db,
        cardiology_dept,
        "Echocardiogram",
        "Ultrasound imaging of the heart.",
        "No special preparation required. Arrive 10 minutes early.",
    )
    skin_check = _get_or_create_service(
        db,
        derm_dept,
        "Full Skin Check",
        "A full-body skin examination.",
        "Avoid wearing makeup or nail polish to the appointment.",
    )

    # These links must exist before the services are published: the
    # workflow reads each service's specialties through them when it
    # builds the chunk text, and a service with no link gets none.
    _get_or_create_provider_service(db, ahmed, knee_xray)
    _get_or_create_provider_service(db, khan, echo)
    _get_or_create_provider_service(db, raza, skin_check)

    _get_or_create_patient(db, "patient.one@example.com", date(1990, 5, 14))
    _get_or_create_patient(db, "patient.two@example.com", date(1985, 11, 2))
    _get_or_create_patient(db, "patient.three@example.com", date(2000, 1, 30))

    return [knee_xray, echo, skin_check]


async def publish_services(
    db: Session, services: list[Service], client: Client | None = None
) -> None:
    """Publish each service through the real publish workflow, one at a
    time, waiting for each to finish, and print where it ended up.

    start_publish() is the same function POST /services/{id}/publish
    calls, so the seed follows the API's rules rather than its own. Its
    status check decides what may be published: on a re-run the services
    are already PUBLISHED, it raises a 409 AppError, and the service is
    left as it is.

    `client` is for tests, which pass their own Temporal test server.
    """
    if client is None:
        client = await get_temporal_client()
    for service in services:
        try:
            _, workflow_id = await service_publish.start_publish(db, service.id)
        except AppError:
            print(f"  {service.name}: already {service.status}, left as it is")
            continue

        # Without a timeout, a stopped temporal-worker would make the seed
        # hang with no output: the workflow is accepted but never runs.
        try:
            await asyncio.wait_for(
                client.get_workflow_handle(workflow_id).result(),
                timeout=PUBLISH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise SystemExit(
                f"{service.name} was still publishing after "
                f"{PUBLISH_TIMEOUT_SECONDS}s. Is temporal-worker running? "
                f"The workflow is still queued; see {workflow_id} in the "
                "Temporal UI."
            ) from None

        db.refresh(service)
        print(f"  {service.name}: {service.status}")


def main() -> None:
    """Entry point for `python -m scripts.seed`.

    Owns the Session -- opening and always closing it -- so seed() itself
    stays agnostic about where its database connection came from, and
    prints the accounts it owns so a live check never has to guess which
    rows in a well-used dev database came from here.
    """
    db = SessionLocal()
    try:
        services = seed(db)
        # seed()'s helpers commit only when they insert something, and
        # several return early when the row already exists -- so a re-run
        # that only *repairs* a row flushes without ever committing, and
        # closing the session throws the repair away. main() owns the
        # session, so it owns the final commit.
        db.commit()
        print("Publishing the seeded services through the publish workflow:")
        asyncio.run(publish_services(db, services))
        print(f"Seed complete. These accounts all share the password {SEED_PASSWORD}:")
        for email in SEED_ACCOUNTS:
            print(f"  {email}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
