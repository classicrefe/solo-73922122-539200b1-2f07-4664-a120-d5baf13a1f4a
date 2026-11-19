-- ============================================================
-- Demo source schema: cyclic referential graph used for the
-- circular-reference worked example in /examples.
--
-- All columns are integer; no patient/real data is included.
--
-- Graph:
--   dept  (mgr_emp)  ──► employee.emp_id        (cycle 1)
--   employee.dept_id ──► dept.dept_id           (cycle 1)
--   employee.mentor_id ─► employee.emp_id       (self reference)
--   project (dept_id, dept_code) MATCH FULL ─► dept (dept_id, dept_code)
--   assignment (emp_id, project_id) ─► employee / project, composite UK
-- ============================================================

DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;

CREATE TABLE dept (
    dept_id    integer NOT NULL,
    dept_code  smallint NOT NULL,
    parent_dept integer,                       -- nullable, self reference below
    mgr_emp    integer,                        -- nullable cycle FK -> employee
    CONSTRAINT dept_pk PRIMARY KEY (dept_id),
    CONSTRAINT dept_code_uk UNIQUE (dept_id, dept_code),
    CONSTRAINT dept_parent_fk FOREIGN KEY (parent_dept)
        REFERENCES dept (dept_id) MATCH SIMPLE
        ON DELETE SET NULL ON UPDATE NO ACTION NOT DEFERRABLE
    -- dept_mgr_fk is added after employee exists (circular reference)
);

CREATE TABLE employee (
    emp_id     integer NOT NULL,
    dept_id    integer NOT NULL,
    mentor_id  integer,                       -- nullable self FK
    level      smallint NOT NULL,
    CONSTRAINT employee_pk PRIMARY KEY (emp_id),
    CONSTRAINT employee_dept_fk FOREIGN KEY (dept_id)
        REFERENCES dept (dept_id) MATCH SIMPLE
        ON DELETE RESTRICT ON UPDATE CASCADE NOT DEFERRABLE,
    CONSTRAINT employee_mentor_fk FOREIGN KEY (mentor_id)
        REFERENCES employee (emp_id) MATCH SIMPLE
        ON DELETE SET NULL ON UPDATE NO ACTION NOT DEFERRABLE
);

CREATE TABLE project (
    project_id bigint NOT NULL,
    dept_id    integer,                       -- part of nullable MATCH FULL key
    dept_code  smallint,
    budget     integer,
    CONSTRAINT project_pk PRIMARY KEY (project_id),
    CONSTRAINT project_dept_fk FOREIGN KEY (dept_id, dept_code)
        REFERENCES dept (dept_id, dept_code) MATCH FULL
        ON DELETE NO ACTION ON UPDATE NO ACTION NOT DEFERRABLE
);

CREATE TABLE assignment (
    emp_id      integer NOT NULL,
    project_id  bigint NOT NULL,
    role_code   integer,                      -- nullable plain column
    CONSTRAINT assignment_pk PRIMARY KEY (emp_id, project_id),
    CONSTRAINT assignment_emp_fk FOREIGN KEY (emp_id)
        REFERENCES employee (emp_id) MATCH SIMPLE
        ON DELETE CASCADE ON UPDATE CASCADE NOT DEFERRABLE,
    CONSTRAINT assignment_project_fk FOREIGN KEY (project_id)
        REFERENCES project (project_id) MATCH SIMPLE
        ON DELETE CASCADE ON UPDATE CASCADE NOT DEFERRABLE
);

-- Close the circular reference dept -> employee -> dept.
ALTER TABLE dept
    ADD CONSTRAINT dept_mgr_fk FOREIGN KEY (mgr_emp)
        REFERENCES employee (emp_id) MATCH SIMPLE
        ON DELETE SET NULL ON UPDATE CASCADE NOT DEFERRABLE;
