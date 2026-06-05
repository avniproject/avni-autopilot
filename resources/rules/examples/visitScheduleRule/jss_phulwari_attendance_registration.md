---
rule_kind: visitScheduleRule
intent: "schedule the first Daily Attendance Form for the registration date — if that's a Sunday move it to the next day, and include the weekday name in the visit title"
entity_param: individual
encounter_types: ["Daily Attendance Form"]
concepts: []
source_org: "JSS"
---
```js
"use strict";
({ params, imports }) => {
  const individual = params.entity;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({
    individual
  });
  const _ = imports.lodash;
  const dailyAttendanceEncounter = _.find(individual.encounters, ({encounterType}) => encounterType.name === 'Daily Attendance Form');
  if(_.isEmpty(dailyAttendanceEncounter)) {
      let earliestVisitDate = imports.moment(individual.registrationDate).toDate();
      let weekDayName =  imports.moment(earliestVisitDate).format('ddd');
      
      if(_.isEqual(weekDayName,'Sun')){
       earliestVisitDate = imports.moment(individual.registrationDate).add(1, 'days').toDate();
         weekDayName =  imports.moment(earliestVisitDate).format('ddd');
       }
      
      scheduleBuilder.add({
                    name: "Daily Attendance" +' -'+weekDayName,
                    encounterType: "Daily Attendance Form",
                    earliestDate: earliestVisitDate,
                    maxDate: earliestVisitDate
                }
            );
    }
            
  return scheduleBuilder.getAll();
};
```
