---
rule_kind: visitScheduleRule
intent: "schedule the first Follow up SCT visit on the enrolment date with a 4-day window, named 'Registry SCT'"
entity_param: programEnrolment
encounter_types: ["Follow up SCT"]
concepts: []
source_org: "IPH Sickle Cell"
---
```js
"use strict";
({ params, imports }) => {
  const programEnrolment = params.entity;
  const moment = imports.moment;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({programEnrolment});
  
  const editFlag = programEnrolment.individual.getEncounters().filter(enc => enc.encounterType.name == 'Follow up SCT').length == 0;
  
  if(editFlag ){
    const earliestDate = moment(programEnrolment.enrolmentDateTime).add(0, 'days').toDate();
    const maxDate = moment(programEnrolment.enrolmentDateTime).add(4, 'days').toDate();
    scheduleBuilder.add({name: "Registry SCT", encounterType: "Follow up SCT", earliestDate, maxDate});  
}
  
  return scheduleBuilder.getAll();
};
```
