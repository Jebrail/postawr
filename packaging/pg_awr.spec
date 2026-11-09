%{!?pgmajor:%global pgmajor 18}
%{!?pgawr_version:%global pgawr_version 1.2}
%{!?pgawr_release:%global pgawr_release 1}
# Keep packages readable by RPM 4.x on RHEL 8/9, even when built with RPM 6.
%global _rpmformat 4
%global _binary_payload w9.gzdio

Name:           pg_awr_%{pgmajor}
Version:        %{pgawr_version}
Release:        %{pgawr_release}%{?dist}
Summary:        Historical PostgreSQL performance statistics and interval reports
# The source project has no public redistribution license.
License:        LicenseRef-Proprietary
Source0:        pg_awr-%{version}.tar.gz
BuildArch:      noarch
Requires:       postgresql%{pgmajor}-server
Requires:       postgresql%{pgmajor}-contrib
Requires:       postgresql%{pgmajor}

%description
SQL/PL/pgSQL extension for PostgreSQL %{pgmajor}. Collect historical statistics
and generate JSON or HTML interval reports. Enable with CREATE EXTENSION
pg_awr CASCADE after preloading pg_stat_statements. Scheduling is optional
and external; no bootstrap installer or dedicated database is required.

%prep
%setup -q -n pg_awr-%{version}

%build
# Pure SQL: no compiler, PostgreSQL headers, or running server needed.

%install
install -d %{buildroot}/usr/pgsql-%{pgmajor}/share/extension
install -m 0644 pg_awr.control sql/pg_awr--*.sql %{buildroot}/usr/pgsql-%{pgmajor}/share/extension/
install -D -m 0755 bin/pg-awr-job %{buildroot}/usr/pgsql-%{pgmajor}/bin/pg-awr-job
install -d %{buildroot}%{_datadir}/doc/%{name}/docs
install -m 0644 README.md packaging/copyright %{buildroot}%{_datadir}/doc/%{name}/
install -m 0644 docs/*.md docs/*.json %{buildroot}%{_datadir}/doc/%{name}/docs/
cp -r examples %{buildroot}%{_datadir}/doc/%{name}/
chmod -R a+rX,u+w,go-w %{buildroot}%{_datadir}/doc/%{name}

%files
%defattr(-,root,root,-)
/usr/pgsql-%{pgmajor}/share/extension/pg_awr.control
/usr/pgsql-%{pgmajor}/share/extension/pg_awr--*.sql
/usr/pgsql-%{pgmajor}/bin/pg-awr-job
%doc %{_datadir}/doc/%{name}
