---
rule_kind: visitScheduleRule
intent: "schedule a Growth Monitoring visit for the enrolment date and an Albendazole visit for the next February or August"
entity_param: programEnrolment
encounter_types: ["Albendazole", "Anthropometry Assessment"]
concepts: []
source_org: "JSS"
---
```js
"use strict";
({params, imports}) => {
    const programEnrolment = params.entity;
    const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({
        programEnrolment
    });
    const moment = imports.moment;
    const FEB = 1;
    const AUG = 7;
    const findSlots = (anyDate) => {
        anyDate = moment(anyDate).startOf('day').toDate();
        if (moment(anyDate).month() < FEB) {
            return moment(anyDate).startOf('month').month(FEB).toDate();
        }
        if (moment(anyDate).month() === FEB) {
            return anyDate;
        }
        if (moment(anyDate).month() < AUG) {
            return moment(anyDate).startOf('month').month(AUG).toDate();
        }
        if (moment(anyDate).month() === AUG) {
            return anyDate;
        }
        return moment(anyDate).add(1, 'year').month(FEB).startOf('month').toDate();
    };
    const getVisitSchedule = (_earliestDate) => {
        let earliestDate = moment(_earliestDate).startOf('day').toDate();
        let maxDate = moment(earliestDate).endOf('month').toDate();
        if (moment(_earliestDate).month() === FEB) {
            return {
                name: 'Albendazole FEB',
                encounterType: 'Albendazole',
                earliestDate, maxDate,
            }
        }
        return {
            name: 'Albendazole AUG',
            encounterType: 'Albendazole',
            earliestDate, maxDate,
        }
    };
    const earliestDate = programEnrolment.enrolmentDateTime;
    const maxDate = programEnrolment.enrolmentDateTime;
    
    scheduleBuilder.add({
            name: "Growth Monitoring Visit",
            encounterType: "Anthropometry Assessment",
            earliestDate: earliestDate,
            maxDate: maxDate
        }
    );
    scheduleBuilder.add(getVisitSchedule(findSlots(programEnrolment.enrolmentDateTime)));
    return scheduleBuilder.getAllUnique("encounterType");
};
```
